"""Harness 持久化仓储 —— 运行快照与审批单的读写。

对外只暴露领域模型（RunSnapshot / ApprovalRequest），SQLAlchemy 细节收敛在此。
所有方法同步执行，调用方在 async 上下文用 ``asyncio.to_thread`` 包裹（见 runtime）。
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from harness.contracts import ApprovalRequest, ApprovalStatus, RunSnapshot, utcnow
from server.database import SessionLocal, init_db
from server.models.harness import HarnessApproval, HarnessRun
from server.utils.logger import logger


class HarnessStore:
    def __init__(self) -> None:
        self._ready = False

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        try:
            init_db()
            self._ready = True
        except Exception as exc:  # 表已存在 / 并发建表都可能抛错，记录后继续
            logger.warning(f"[HarnessStore] init_db skipped: {exc}")
            self._ready = True

    # ------------------------------------------------------------------ runs
    def save_run(self, snapshot: RunSnapshot) -> None:
        self._ensure_schema()
        snapshot.updated_at = utcnow()
        data = snapshot.model_dump(mode="json")
        db = SessionLocal()
        try:
            row = db.get(HarnessRun, snapshot.id)
            if row is None:
                row = HarnessRun(
                    id=snapshot.id,
                    user_id=snapshot.user_id,
                    objective=snapshot.objective,
                    status=str(snapshot.status),
                    mode=snapshot.mode,
                    snapshot=data,
                )
                db.add(row)
            else:
                row.user_id = snapshot.user_id
                row.objective = snapshot.objective
                row.status = str(snapshot.status)
                row.mode = snapshot.mode
                row.snapshot = data
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error(f"[HarnessStore] save_run failed: {exc}")
        finally:
            db.close()

    def get_run(self, run_id: str) -> RunSnapshot | None:
        self._ensure_schema()
        db = SessionLocal()
        try:
            row = db.get(HarnessRun, run_id)
            if row is None:
                return None
            return RunSnapshot.model_validate(row.snapshot)
        except SQLAlchemyError as exc:
            logger.error(f"[HarnessStore] get_run failed: {exc}")
            return None
        finally:
            db.close()

    def list_runs(self, user_id: int | None = None, limit: int = 50) -> list[RunSnapshot]:
        self._ensure_schema()
        db = SessionLocal()
        try:
            stmt = select(HarnessRun).order_by(HarnessRun.created_at.desc()).limit(limit)
            if user_id is not None:
                stmt = stmt.where(HarnessRun.user_id == user_id)
            rows = db.execute(stmt).scalars().all()
            return [RunSnapshot.model_validate(r.snapshot) for r in rows]
        except SQLAlchemyError as exc:
            logger.error(f"[HarnessStore] list_runs failed: {exc}")
            return []
        finally:
            db.close()

    def delete_run(self, run_id: str) -> bool:
        self._ensure_schema()
        db = SessionLocal()
        try:
            row = db.get(HarnessRun, run_id)
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error(f"[HarnessStore] delete_run failed: {exc}")
            return False
        finally:
            db.close()

    # ------------------------------------------------------------- approvals
    def save_approval(self, approval: ApprovalRequest) -> None:
        self._ensure_schema()
        data = approval.model_dump(mode="json")
        db = SessionLocal()
        try:
            row = db.get(HarnessApproval, approval.id)
            if row is None:
                row = HarnessApproval(
                    id=approval.id,
                    run_id=approval.run_id,
                    task_id=approval.task_id,
                    action=approval.action,
                    target=approval.target,
                    risk=str(approval.risk),
                    status=str(approval.status),  # ApprovalRequest 已 use_enum_values，为字符串
                    data=data,
                    expires_at=approval.expires_at,
                )
                db.add(row)
            else:
                row.status = str(approval.status)
                row.risk = str(approval.risk)
                row.data = data
                row.decided_at = approval.decided_at
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error(f"[HarnessStore] save_approval failed: {exc}")
        finally:
            db.close()

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        self._ensure_schema()
        db = SessionLocal()
        try:
            row = db.get(HarnessApproval, approval_id)
            return ApprovalRequest.model_validate(row.data) if row else None
        except SQLAlchemyError as exc:
            logger.error(f"[HarnessStore] get_approval failed: {exc}")
            return None
        finally:
            db.close()

    def list_approvals(
        self,
        run_id: str | None = None,
        status: ApprovalStatus | str | None = None,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        self._ensure_schema()
        db = SessionLocal()
        try:
            stmt = select(HarnessApproval).order_by(HarnessApproval.created_at.desc()).limit(limit)
            if run_id:
                stmt = stmt.where(HarnessApproval.run_id == run_id)
            if status:
                status_value = status.value if isinstance(status, Enum) else str(status)
                stmt = stmt.where(HarnessApproval.status == status_value)
            if user_id is not None:
                stmt = stmt.join(HarnessRun, HarnessRun.id == HarnessApproval.run_id).where(
                    HarnessRun.user_id == user_id
                )
            rows = db.execute(stmt).scalars().all()
            return [ApprovalRequest.model_validate(r.data) for r in rows]
        except SQLAlchemyError as exc:
            logger.error(f"[HarnessStore] list_approvals failed: {exc}")
            return []
        finally:
            db.close()


store = HarnessStore()
