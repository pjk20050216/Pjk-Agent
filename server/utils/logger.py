"""日志模块 —— 配置控制台 + 按日滚动的文件日志，供全应用统一使用。"""
import logging
import sys
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def get_logger(name: str = "agent") -> logging.Logger:
    """获取配置好的 logger 实例：控制台输出 INFO 级别，文件输出 DEBUG 级别（按日期命名）。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # 控制台处理器：INFO 级别，简洁格式
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter("%(levelname)s - %(message)s")
    console_handler.setFormatter(console_fmt)

    # 文件处理器：DEBUG 级别，含时间戳，每天一个文件
    log_file = LOG_DIR / f"agent_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(file_fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


# 全局 logger 实例，各模块直接导入使用
logger = get_logger()
