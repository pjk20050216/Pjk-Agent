"""认证依赖模块 —— 双模式：网关信任（内部令牌 + X-User-ID）或直接 JWT Bearer Token 解析。"""
import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from server.config import settings
from server.database import get_db
from server.models.user import User
from server.utils.security import decode_token

security_optional = HTTPBearer(auto_error=False)

# 允许触发「网关信任」模式的来源地址
TRUSTED_HOSTS = ("127.0.0.1", "::1", "localhost")


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_optional),
    db: Session = Depends(get_db),
) -> User:
    """认证用户身份。两种模式：
    1. 网关模式：请求来自 localhost **且** 携带正确的内部令牌 + X-User-ID → 信任该头，直接查 DB
    2. 直接模式：验证 Authorization: Bearer <token> 中的 JWT Token，然后再查 DB

    仅凭「来源是 localhost」不足以信任 X-User-ID：本机任意进程都能连接 127.0.0.1，
    且若后端端口被直接暴露，攻击者可自行伪造该头冒充任意用户。因此要求内部令牌，
    该令牌只由网关注入（网关会先剔除调用方自带的同名头）。
    """
    client_host = request.client.host if request.client else ""

    # 网关模式：localhost 来源 + 内部令牌校验通过
    if client_host in TRUSTED_HOSTS:
        user_id_str = request.headers.get("X-User-ID")
        presented = request.headers.get("X-Internal-Token", "")

        # 常量时间比较，避免通过响应时间差逐字节猜测令牌
        if user_id_str and hmac.compare_digest(presented, settings.internal_token):
            try:
                user_id = int(user_id_str)
            except ValueError:
                raise HTTPException(status_code=401, detail="Invalid X-User-ID")
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                return user
            raise HTTPException(status_code=401, detail="User not found")

    # 直接模式：验证 Bearer JWT Token
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        payload = decode_token(credentials.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user
