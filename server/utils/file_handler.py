"""文件处理工具 —— SHA-256 哈希计算、文件类型校验、目录扫描。"""
import hashlib
from pathlib import Path
from server.utils.logger import logger

ALLOWED_TYPES = (".txt", ".pdf")


def get_file_sha256(filepath: Path) -> str:
    """计算文件的 SHA-256 哈希值（分块读取以支持大文件）。"""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def listdir_with_allowed_type(path: Path, allowed_types: tuple = ALLOWED_TYPES) -> list[Path]:
    """列出目录中所有指定类型的文件。"""
    files = []
    if not path.is_dir():
        logger.warning(f"Not a directory: {path}")
        return files
    for f in path.iterdir():
        if f.is_file() and f.suffix.lower() in allowed_types:
            files.append(f)
    return files


def validate_file_type(filename: str, allowed_types: list[str] | None = None) -> bool:
    """根据文件扩展名判断是否属于允许上传的类型。"""
    if allowed_types is None:
        allowed_types = ["pdf", "txt"]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in allowed_types
