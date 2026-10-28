"""虚拟文件系统。"""
from harness.vfs.hooked import HookedFileSystem
from harness.vfs.store import (
    VFSError,
    VFSNotFound,
    VFSPermissionError,
    VFSTooLarge,
    VirtualFileSystem,
)

__all__ = [
    "HookedFileSystem",
    "VirtualFileSystem",
    "VFSError",
    "VFSNotFound",
    "VFSPermissionError",
    "VFSTooLarge",
]
