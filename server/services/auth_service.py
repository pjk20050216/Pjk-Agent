"""认证业务逻辑 —— 实现用户注册、登录、Token 刷新的核心逻辑。"""
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from server.models.user import User
from server.utils.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token


def register(db: Session, username: str, email: str, password: str) -> dict:
    """用户注册：校验用户名/邮箱唯一性，哈希密码后存入数据库，返回用户信息 + JWT Token 对。"""
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already exists")

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = create_access_token(user.id)
    # 生成新的 token 字符串
    refresh_token = create_refresh_token(user.id)
    return {
        "user": user,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


def login(db: Session, username: str, password: str) -> dict:
    """用户登录：验证用户名和密码，成功后返回用户信息 + JWT Token 对。"""
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    return {
        "user": user,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


# 一个完整的业务逻辑，用于执行 token 刷新的完整流程，也就是当 access_token 过期时
def refresh_access_token(token_str: str) -> dict:
    """Token 刷新：验证 refresh_token 有效性后，颁发新的 access_token + refresh_token 对。"""
    try:
        # 验证
        payload = decode_token(token_str)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id = int(payload["sub"])
    access_token = create_access_token(user_id)
    refresh_token = create_refresh_token(user_id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
