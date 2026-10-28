"""中间件协议 —— 在模型调用、工具调用、文件读写、任务状态变更等节点注入 Hook。

执行模型（洋葱式）：
1. 依 ``order`` 升序执行命中的 ``before`` 钩子；任一钩子可设置 ``short_circuit``
   （缓存命中）或抛 :class:`HookDenied`（权限/敏感拦截）；
2. 执行实际操作（被 ``around`` 中间件包裹，可实现重试、超时、限流）；
3. 逆序执行 ``after`` 钩子（审计、追踪、指标）；
4. 操作抛错时，``after`` 仍会执行并携带 ``ctx.error``，随后异常继续上抛。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from harness.contracts import HookPoint, RiskLevel


class HookDenied(Exception):
    """中间件拒绝执行。"""

    def __init__(self, reason: str, *, code: str = "denied", risk: RiskLevel | str = RiskLevel.LOW):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.risk = risk


class ApprovalRequired(Exception):
    """需要人工审批；由审批中间件内部处理，一般不冒泡到 Agent。"""

    def __init__(self, action: str, target: str = "", reason: str = ""):
        super().__init__(reason or action)
        self.action = action
        self.target = target
        self.reason = reason


@dataclass
class MiddlewareContext:
    run_id: str
    hook: HookPoint
    payload: dict[str, Any] = field(default_factory=dict)
    user_id: int | None = None
    agent: str = ""
    task_id: str | None = None
    services: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: BaseException | None = None
    short_circuit: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class Middleware:
    """中间件基类：按需声明关心哪些 Hook。"""

    name: str = "middleware"
    order: int = 100
    before_hooks: set[HookPoint] = set()
    after_hooks: set[HookPoint] = set()

    async def before(self, ctx: MiddlewareContext) -> None:
        return None

    async def after(self, ctx: MiddlewareContext) -> None:
        return None

    async def around(
        self,
        ctx: MiddlewareContext,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        return await call()


class MiddlewareChain:
    def __init__(self, middlewares: list[Middleware] | None = None) -> None:
        self._middlewares = sorted(middlewares or [], key=lambda m: m.order)

    @property
    def middlewares(self) -> list[Middleware]:
        return list(self._middlewares)

    def add(self, middleware: Middleware) -> "MiddlewareChain":
        self._middlewares.append(middleware)
        self._middlewares.sort(key=lambda m: m.order)
        return self

    async def run(
        self,
        ctx: MiddlewareContext,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        before_hook = ctx.hook
        after_hook = paired_after(before_hook)
        ctx.metadata["before_hook"] = before_hook.value
        active_before = [m for m in self._middlewares if before_hook in m.before_hooks]
        active_after = [m for m in self._middlewares if after_hook in m.after_hooks]
        active_around = list(active_before)

        try:
            for middleware in active_before:
                await middleware.before(ctx)
                if ctx.short_circuit:
                    break

            if ctx.short_circuit:
                ctx.result = ctx.result if ctx.result is not None else ctx.state.get("cached")
            else:
                wrapped: Callable[[], Awaitable[Any]] = operation
                for middleware in reversed(active_around):
                    wrapped = _wrap(middleware, ctx, wrapped)
                ctx.result = await wrapped()
            return ctx.result
        except BaseException as exc:
            ctx.error = exc
            raise
        finally:
            # after 阶段使用配对的 AFTER_* 钩子，便于审计/追踪区分阶段
            ctx.hook = after_hook
            for middleware in reversed(active_after):
                try:
                    await middleware.after(ctx)
                except Exception:
                    # 审计/追踪失败不应掩盖真实业务异常
                    pass


def paired_after(hook: HookPoint) -> HookPoint:
    """把 BEFORE_X 映射为 AFTER_X；RUN_START 覆盖整个运行，映射为 RUN_END。"""
    if hook == HookPoint.RUN_START:
        return HookPoint.RUN_END
    if hook.value.startswith("before_"):
        try:
            return HookPoint("after_" + hook.value[len("before_"):])
        except ValueError:
            return hook
    return hook


def _wrap(
    middleware: Middleware,
    ctx: MiddlewareContext,
    inner: Callable[[], Awaitable[Any]],
) -> Callable[[], Awaitable[Any]]:
    async def _call() -> Any:
        return await middleware.around(ctx, inner)

    return _call


def build_context(
    run_id: str,
    hook: HookPoint,
    *,
    payload: dict | None = None,
    user_id: int | None = None,
    agent: str = "",
    task_id: str | None = None,
    services: dict | None = None,
    state: dict | None = None,
) -> MiddlewareContext:
    return MiddlewareContext(
        run_id=run_id,
        hook=hook,
        payload=payload or {},
        user_id=user_id,
        agent=agent,
        task_id=task_id,
        services=services or {},
        state=state or {},
    )
