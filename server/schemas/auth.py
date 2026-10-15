"""认证相关 Pydantic 模型 —— 定义注册/登录请求、Token 响应、用户信息的请求体和响应体结构。"""
from pydantic import BaseModel, EmailStr


# BaseModel 对应 Spring Boot 中的 @RequestBody 接收的 DTO
# 在 Spring Boot 中，我们通常会定义一个普通的 Java 类（DTO）来接收前端传来的 JSON 数据，
# 并在 Controller 方法参数前加上 @RequestBody 注解，让 Jackson 框架自动将 JSON 反序列化为该对象。
class RegisterRequest(BaseModel):
    """注册请求体：用户名、邮箱、密码。"""
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    """登录请求体：用户名、密码。"""
    username: str
    password: str


class TokenResponse(BaseModel):
    """Token 响应：access_token（短期）+ refresh_token（长期），用于前端请求认证。"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """刷新 Token 请求体：携带 refresh_token 换取新的 Token 对。"""
    refresh_token: str


class UserResponse(BaseModel):
    """用户信息响应：不包含密码哈希，安全返回给前端。"""
    id: int
    username: str
    email: str
    avatar_url: str | None = None

    # 类似于 Spring Boot 中将 entity 中的一部分内容拷贝到 DTO 中
    class Config:
        # 允许模型直接读取 ORM 对象（如 SQLAlchemy 模型）的属性，相当于内置了安全的对象属性映射转换器，省去了手动拷贝字段的步骤。
        from_attributes = True


class AuthResponse(BaseModel):
    """注册/登录的统一响应：用户信息 + Token 对。"""
    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
