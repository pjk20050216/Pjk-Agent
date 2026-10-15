"""认证路由 —— 处理用户注册、登录、Token 刷新和当前用户信息查询。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from server.database import get_db
from server.dependencies import get_current_user
from server.models.user import User
from server.schemas.auth import (
    RegisterRequest, LoginRequest, RefreshRequest,
    AuthResponse, TokenResponse, UserResponse,
)
from server.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    """POST /api/auth/register —— 用户注册，返回用户信息和 JWT Token 对。"""
    result = auth_service.register(db, req.username, req.email, req.password)
    return AuthResponse(
        user=UserResponse.model_validate(result["user"]),
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """POST /api/auth/login —— 用户名+密码登录，返回用户信息和 JWT Token 对。"""
    result = auth_service.login(db, req.username, req.password)
    return AuthResponse(
        user=UserResponse.model_validate(result["user"]),
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(req: RefreshRequest):
    """POST /api/auth/refresh —— 用 refresh_token 换取新的 Token 对（无需数据库查询）。"""
    result = auth_service.refresh_access_token(req.refresh_token)
    return TokenResponse(**result)


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    """GET /api/auth/me —— 获取当前登录用户的个人信息（需携带 access_token）。"""
    return UserResponse.model_validate(current_user)
