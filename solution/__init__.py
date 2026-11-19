"""本地联邦身份目录:离线、纯标准库实现。"""
from .engine import DirectoryEngine, Reject, make_confirmation

__all__ = ["DirectoryEngine", "Reject", "make_confirmation"]
__version__ = "1.0.0"
