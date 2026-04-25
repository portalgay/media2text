# -*- coding: utf-8 -*-
"""大文件分块计算 SHA-256 前缀，用于对象存储去重等。"""

import hashlib
from pathlib import Path
from typing import Union


def sha256_file_prefix8(path: Union[str, Path], chunk: int = 1024 * 1024) -> str:
    """对本地文件内容计算 SHA-256，取前 8 个十六进制字符（小写）。"""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()[:8].lower()


def sha256_bytes_prefix8(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8].lower()
