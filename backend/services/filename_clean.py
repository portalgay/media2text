# -*- coding: utf-8 -*-
"""按 Redis 配置清洗文件名（无后缀主名）：固定替换 → 模板正则（顺序）→ 设置中单条正则。"""

import re
from pathlib import Path
from typing import Any, Dict, List

from core.logger import get_logger

LOG = get_logger("filename_clean")

# OSS / 本地路径中不宜出现的字符
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00\r\n]')


def safe_display_stem(stem: str) -> str:
    """将清洗后 stem 中不安全字符替换为下划线，避免路径异常。"""
    t = _UNSAFE.sub("_", (stem or "").strip())
    t = t.strip(" .")
    return t or "unnamed"


def _stem_from_original_filename(original_filename: str) -> str:
    return Path(
        (original_filename or "unnamed").replace("\\", "/").split("/")[-1]
    ).stem


def clean_stem_by_regex_pattern(
    stem: str,
    pattern: str,
    *,
    trace_id: str = "-",
) -> str:
    """
    对主名按单条 Python 正则清洗（与 legacy filename_clean_regex 语义一致）。

    - pattern 为空：返回 stem
    - 有捕获组且匹配成功：取第一个非空捕获组
    - 无捕获组或第一组为空：将匹配子串替换为 '' 后的结果
    - 正则不合法 / 无匹配 / 结果为空：回退 stem，并打 warning
    """
    stem = stem or ""
    p = (pattern or "").strip()
    if not p:
        return stem
    try:
        rx = re.compile(p)
    except re.error as e:
        LOG.warning(
            "filename 正则非法，已忽略 | pattern=%r | %s | trace=%s",
            p,
            e,
            trace_id,
            extra={"trace_id": trace_id},
        )
        return stem
    m = rx.search(stem)
    if not m:
        LOG.warning(
            "filename 正则未匹配，已保持当前主名 | stem=%r | pattern=%r | trace=%s",
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
            "filename 正则清洗后为空，已保持当前主名 | stem=%r | pattern=%r | trace=%s",
            stem,
            p,
            trace_id,
            extra={"trace_id": trace_id},
        )
        return stem
    return out


def clean_file_stem(
    original_filename: str,
    pattern: str,
    *,
    trace_id: str = "-",
) -> str:
    """
    对 original_filename 去掉扩展名后得到的主名，按 Python 正则做清洗。
    （保留兼容：仅单条 pattern；新逻辑请用 clean_file_stem_from_config。）
    """
    stem = _stem_from_original_filename(original_filename)
    return clean_stem_by_regex_pattern(stem, pattern, trace_id=trace_id)


def _cfg_list(cfg: Any, key: str) -> list:
    if isinstance(cfg, dict):
        v = cfg.get(key)
    else:
        v = getattr(cfg, key, None)
    return v if isinstance(v, list) else []


def _apply_temp_rules(stem: str, rules: List[str], *, trace_id: str = "-") -> str:
    """顺序执行「旧->新」字面量替换（非正则）。"""
    out = stem or ""
    for line in rules or []:
        s = (line or "").strip()
        if not s or "->" not in s:
            continue
        old, new = s.split("->", 1)
        prev = out
        out = out.replace(old, new)
        if prev != out:
            LOG.info(
                "文件名清洗 | %r → %r | 固定替换: %r→%r",
                prev, out, old, new or "(删除)",
                extra={"trace_id": trace_id},
            )
    return out


def _apply_library_rule(stem: str, rule: Dict[str, Any], trace_id: str) -> str:
    """单条模板：replace 为 re.sub；extract 与 legacy 捕获组语义一致。"""
    prev = stem or ""
    p = (rule.get("pattern") or "").strip()
    if not p:
        return prev
    rtype = (rule.get("rule_type") or "replace").strip().lower()
    rule_name = rule.get("name", "未命名")
    
    if rtype == "extract":
        result = clean_stem_by_regex_pattern(prev, p, trace_id=trace_id)
        if result != prev:
            LOG.info(
                "文件名清洗 | %r → %r | [%s] 提取: %s",
                prev, result, rule_name, p,
                extra={"trace_id": trace_id},
            )
        return result
    
    try:
        rx = re.compile(p)
    except re.error as e:
        LOG.warning(
            "文件名清洗 | 正则非法跳过 | pattern=%r | %s",
            p, e,
            extra={"trace_id": trace_id},
        )
        return prev
    repl = rule.get("replacement")
    if repl is None:
        repl = ""
    new = rx.sub(str(repl), prev)
    new = (new or "").strip()
    if not new:
        LOG.warning(
            "文件名清洗 | 替换后为空，保持原样 | stem=%r",
            prev,
            extra={"trace_id": trace_id},
        )
        return prev
    
    if new != prev:
        LOG.info(
            "文件名清洗 | %r → %r | [%s] %s → %r",
            prev, new, rule_name, p, repl or "(删除)",
            extra={"trace_id": trace_id},
        )
    return new


def clean_file_stem_from_config(
    original_filename: str,
    cfg: Any,
    *,
    trace_id: str = "-",
) -> str:
    """
    完整清洗：主名 → 固定替换 → 按选中顺序套用模板 → 设置中 filename_clean_regex。
    """
    stem = _stem_from_original_filename(original_filename)
    
    # 第1步：固定替换
    stem = _apply_temp_rules(stem, _cfg_list(cfg, "filename_temp_rules"), trace_id=trace_id)

    # 第2步：正则模板库
    lib = _cfg_list(cfg, "filename_regex_library")
    id_to_rule: Dict[str, Dict[str, Any]] = {}
    for r in lib:
        if not r or not isinstance(r, dict):
            continue
        rid = r.get("id")
        if rid is None:
            continue
        id_to_rule[str(rid)] = r

    for rid in _cfg_list(cfg, "filename_selected_regex_ids"):
        rule = id_to_rule.get(str(rid))
        if not rule or rule.get("enabled") is False:
            continue
        stem = _apply_library_rule(stem, rule, trace_id)

    # 第3步：遗留的单条正则
    legacy = ""
    if isinstance(cfg, dict):
        legacy = (cfg.get("filename_clean_regex") or "").strip()
    else:
        legacy = (getattr(cfg, "filename_clean_regex", "") or "").strip()
    if legacy:
        prev_stem = stem
        stem = clean_stem_by_regex_pattern(stem, legacy, trace_id=trace_id)
        if stem != prev_stem:
            LOG.info(
                "文件名清洗 | %r → %r | 遗留正则: %r",
                prev_stem, stem, legacy,
                extra={"trace_id": trace_id},
            )

    return stem
