"""沙箱命令风险策略 —— 在真正执行前做静态拦截。

策略分层：
1. 高危模式（下载执行、反弹 shell、破坏性删除、提权）→ 直接拒绝；
2. 命令白名单 → 未登记的命令拒绝；
3. 其余命令按风险分级，供审批中间件决定是否需要人工确认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from harness.config import SandboxConfig
from harness.contracts import RiskLevel


@dataclass
class PolicyDecision:
    allowed: bool
    risk: RiskLevel
    reason: str = ""
    action: str = "sandbox.exec"
    matched_rule: str = ""


class SandboxPolicy:
    def __init__(self, config: SandboxConfig) -> None:
        self.config = config
        self._denied = [re.compile(p, re.IGNORECASE) for p in config.denied_patterns]
        self._allowed = {c.lower() for c in config.allowed_commands}

    def first_token(self, command: str) -> str:
        stripped = command.strip()
        if not stripped:
            return ""
        match = re.match(r'^["\']?([^\s"\']+)', stripped)
        token = match.group(1) if match else stripped
        # 去掉路径前缀，如 C:\Python\python.exe -> python.exe
        token = re.split(r"[\\/]", token)[-1]
        return token.lower()

    def check(self, command: str) -> PolicyDecision:
        if not command or not command.strip():
            return PolicyDecision(False, RiskLevel.LOW, "empty command")

        for pattern in self._denied:
            if pattern.search(command):
                return PolicyDecision(
                    False,
                    RiskLevel.CRITICAL,
                    f"command rejected by policy: /{pattern.pattern}/",
                    matched_rule=pattern.pattern,
                )

        token = self.first_token(command)
        if not token:
            return PolicyDecision(False, RiskLevel.LOW, "unable to parse command")

        # 允许带 .exe 后缀（Windows）与常见解释器
        normalized = token[:-4] if token.endswith(".exe") else token
        if normalized not in self._allowed and token not in self._allowed:
            return PolicyDecision(
                False,
                RiskLevel.HIGH,
                f"command '{token}' is not in sandbox allowlist",
            )

        if normalized in {"python", "python3", "pytest"}:
            risk = RiskLevel.MEDIUM
        elif normalized in {"cat", "ls", "dir", "echo", "type", "findstr", "grep"}:
            risk = RiskLevel.LOW
        else:
            risk = RiskLevel.MEDIUM

        return PolicyDecision(True, risk, "allowed", matched_rule=normalized)

    def describe(self) -> dict:
        return {
            "allowed_commands": sorted(self._allowed),
            "denied_patterns": [p.pattern for p in self._denied],
            "allow_network": self.config.allow_network,
            "timeout": self.config.command_timeout,
        }
