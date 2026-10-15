"""安全工具模块 —— bcrypt 密码哈希验证 + JWT Token 的创建与解码。"""
from datetime import datetime, timedelta, timezone
from jose import jwt
import bcrypt
from server.config import settings


def hash_password(password: str) -> str:
    """用 bcrypt 对密码进行加盐哈希（截断至 72 字节以符合 bcrypt 限制）。"""
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码与 bcrypt 哈希是否匹配。"""
    return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    """创建短期 access_token（默认 30 分钟过期），用于 API 请求认证。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    """创建长期 refresh_token（默认 7 天过期），用于刷新 access_token。"""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

"""解码并验证 JWT Token，返回 payload 字典。"""
def decode_token(token: str) -> dict:
    # settings.jwt_secret_key（密钥验证）：验证 Token 真伪的。JWT 在生成时，使用这个密钥配合算法生成了防伪的“签名”。
    # 在解码时，库会使用同样的密钥重新计算签名，并与 Token 自带的签名进行比对。如果密钥一致且数据未被篡改，验证通过。
    # algorithms=[settings.jwt_algorithm]（算法防攻击验证）：指定解码时使用的加密算法（比如 HS256）
    # 用于防止 JWT 著名的“算法混淆攻击”（攻击者将算法改为 none 绕过签名验证，或改用 RS256/HS256 算法错配来伪造 Token）。
    # 明确指定算法后，库只会用该算法进行验证，杜绝了此类安全隐患。
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
