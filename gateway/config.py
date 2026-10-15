"""网关配置 —— 复用 server 的 JWT 密钥等共享配置，不引入数据库/SQLAlchemy 依赖。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.config import settings as server_settings

# 共享的 JWT 配置
JWT_SECRET_KEY: str = server_settings.jwt_secret_key
JWT_ALGORITHM: str = server_settings.jwt_algorithm

# 网关↔后端内部信任令牌（与后端同源派生，保证两端一致）
INTERNAL_TOKEN: str = server_settings.internal_token

# 后端上游地址
BACKEND_URL: str = "http://127.0.0.1:8001"

# 限流参数
RATE_LIMIT_AUTHENTICATED: str = server_settings.rate_limit_authenticated
RATE_LIMIT_UNAUTHENTICATED: str = server_settings.rate_limit_unauthenticated
LLM_CONCURRENCY_LIMIT: int = server_settings.llm_concurrency_limit
