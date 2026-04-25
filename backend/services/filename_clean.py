# -*- coding: utf-8 -*-
"""按 Redis 配置的正则清洗文件名（无后缀主名）。"""

import re
from pathlib import Path
from core.logger import get_logger

LOG = get_logger("filename_clean")

# OSS / 本地路径中不宜出现的字符
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00\r\n]')


def safe_display_stem(stem: str) -> str:
    """将清洗后 stem 中不安全字符替换为下划线，避免路径异常。"""
    t = _UNSAFE.sub("_", (stem or "").strip())
    t = t.strip(" .")
    return t or "unnamed"


def clean_file_stem(
    original_filename: str,
    pattern: str,
    *,
    trace_id: str = "-",
) -> str:
    """
    对 original_filename 去掉扩展名后得到的主名，按 Python 正则做清洗。

    - pattern 为空：返回原主名
    - 有捕获组且匹配成功：取第一个非空捕获组
    - 无捕获组或第一组为空：将 pattern 的匹配子串替换为 '' 后的结果
    - 正则不合法 / 无匹配 / 结果为空：回退原主名，并打 warning
    """
    stem = Path((original_filename or "unnamed").replace("\\", "/").split("/")[-1]).stem
    p = (pattern or "").strip()
    if not p:
        return stem
    try:
        rx = re.compile(p)
    except re.error as e:
        LOG.warning(
            "filename_clean_regex 非法，已忽略 | pattern=%r | %s | trace=%s",
            p,
            e,
            trace_id,
            extra={"trace_id": trace_id},
        )
        return stem
    m = rx.search(stem)
    if not m:
        LOG.warning(
            "filename_clean_regex 未匹配，已使用原主名 | stem=%r | pattern=%r | trace=%s",
            stem,
            p,
            trace_id,
            extra={"trace_id": trace_id},
        )
        return stem
    if m.lastindex and m.lastindex >= 1:
        g1 = m.group(1)
        if g1 is not None and str(g1).strip() != "":
            return str(g1).strip()
    out = rx.sub("", stem)
    out = out.strip()
    if not out:
        LOG.warning(
            "filename_clean_regex 清洗后为空，已使用原主名 | stem=%r | pattern=%r | trace=%s",
            stem,
            p,
            trace_id,
            extra={"trace_id": trace_id},
        )
        return stem
    return out
