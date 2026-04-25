# -*- coding: utf-8 -*-
"""内嵌文字幕轨探测与提取：ffprobe 选轨 + ffmpeg 出 SRT 再解析为纯文本。"""

import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger

LOG = get_logger("subtitle_extract")

# 图片 / 位图类字幕，跳过
BITMAP_SUBTITLE_CODECS = frozenset(
    {
        "hdmv_pgs_subtitle",
        "pgssub",
        "dvd_subtitle",
        "dvb_subtitle",
        "dvb_teletext",
        "xsub",
        "dvb_sub",
    }
)

# 仅处理常见文本轨
_TEXT_SUB_RE = re.compile(
    r"^(subrip|ass|ssa|webvtt|srt|mov_text|text|subviewer|jacosub|pjs|mpsub|realtext|sammi|sami|vplayer|scc)$",
    re.I,
)

_PUNCT_RE = re.compile(
    r"[。，、；：？！\.,;:!?·…《》「」""''（）\(\)——\-]"
)


def _is_text_sub_codec(name: str) -> bool:
    c = (name or "").strip().lower()
    if not c or c in BITMAP_SUBTITLE_CODECS:
        return False
    return bool(_TEXT_SUB_RE.match(c) or c in ("srt", "ass", "vtt", "subrip", "webvtt", "mov_text"))


def _ffprobe_streams(path: Path, *, trace_id: str = "") -> Optional[Dict[str, Any]]:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-select_streams",
                "s",
                str(path.resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            LOG.info(
                "ffprobe 字幕流探测失败 returncode=%s stderr=%s | file=%s",
                r.returncode,
                (r.stderr or "").strip()[:400],
                path.name,
                extra={"trace_id": trace_id or "-"},
            )
            return None
        return json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        LOG.info(
            "ffprobe 字幕流探测异常 %s | file=%s",
            e,
            path.name,
            extra={"trace_id": trace_id or "-"},
        )
        return None


def _is_chinese_sub(lang: str) -> bool:
    s = (lang or "").lower()
    for p in (
        "zh",
        "chi",
        "cmn",
        "zho",
        "yue",
        "chinese",
        "hans",
        "hant",
        "tw",
    ):
        if p in s or s == p:
            return True
    return False


def _pick_subtitle_stream_index(
    streams: List[Dict[str, Any]], *, trace_id: str = ""
) -> Optional[int]:
    """返回选中的流在文件中的 index（与 ffmpeg -map 0:N 中 N 一致）。"""
    cands: List[Tuple[int, int]] = []
    sub_lines: List[str] = []
    for st in streams or []:
        if (st or {}).get("codec_type") != "subtitle":
            continue
        cn = (st.get("codec_name") or st.get("codec_tag_string") or "").strip()
        sidx = st.get("index")
        tags = st.get("tags") or {}
        lang = (tags.get("language") or tags.get("lang") or "") or ""
        line = f"index={sidx} codec={cn!r} lang={lang!r}"
        if not _is_text_sub_codec(cn):
            if cn or sidx is not None:
                reason = "位图/非文本 codec" if cn in BITMAP_SUBTITLE_CODECS else "非白名单文本 codec"
                sub_lines.append(f"{line} ({reason}, 跳过)")
            continue
        if sidx is None:
            sub_lines.append(f"{line} (无 index, 跳过)")
            continue
        pri = 0 if _is_chinese_sub(lang) else 1
        cands.append((pri, int(sidx)))
        sub_lines.append(f"{line} (候选, pri={pri})")
    if sub_lines:
        LOG.info(
            "ffprobe 字幕流一览 | %s",
            " | ".join(sub_lines),
            extra={"trace_id": trace_id or "-"},
        )
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    chosen = cands[0][1]
    LOG.info(
        "选用字幕流 index=%s（优先中文语言标签）",
        chosen,
        extra={"trace_id": trace_id or "-"},
    )
    return chosen


def srt_to_plain_text(raw: str) -> str:
    """SRT/类似块结构：去时间行、去序号、去简单样式标签，合并为纯文本。"""
    if not (raw or "").strip():
        return ""
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n+", t)
    lines_out: List[str] = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        # 去掉纯数字行（序号）
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        # 去时间轴行 00:00:00,000 --> 00:00:00,000
        if re.match(
            r"^\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}",
            lines[0].strip(),
        ):
            lines = lines[1:]
        for ln in lines:
            s = re.sub(r"<[^>]+>", " ", ln)  # 简单去标签
            s = re.sub(r"\s+", " ", s).strip()
            if s:
                lines_out.append(s)
    text = " ".join(lines_out)
    text = re.sub(r"[\s\u3000]+", " ", text).strip()
    return text


def _needs_punctuation_guess(text: str) -> bool:
    if not text or len(text) < 8:
        return False
    n = len(text)
    punct = len(_PUNCT_RE.findall(text))
    ch = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if ch < 8:
        return False
    return (punct / max(n, 1)) < 0.008


def _extract_srt_to_temp(media: Path, *, trace_id: str = "") -> Optional[str]:
    LOG.info(
        "字幕优先：尝试内嵌字幕提取 | file=%s",
        media.name,
        extra={"trace_id": trace_id or "-"},
    )
    data = _ffprobe_streams(media, trace_id=trace_id)
    if not data:
        LOG.info(
            "字幕优先：ffprobe 无有效 JSON 或未探测到流 | file=%s",
            media.name,
            extra={"trace_id": trace_id or "-"},
        )
        return None
    streams = data.get("streams") or []
    LOG.info(
        "字幕优先：ffprobe 返回 subtitle 流条目数=%d | file=%s",
        len(streams),
        media.name,
        extra={"trace_id": trace_id or "-"},
    )
    sidx = _pick_subtitle_stream_index(streams, trace_id=trace_id)
    if sidx is None:
        LOG.info(
            "字幕优先：无可用文字幕轨，将回退 ASR | file=%s",
            media.name,
            extra={"trace_id": trace_id or "-"},
        )
        return None
    with tempfile.TemporaryDirectory(prefix="m2t_sub_") as tmp:
        out = Path(tmp) / "out.srt"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(media.resolve()),
            "-map",
            f"0:{sidx}",
            "-c:s",
            "srt",
            str(out),
            "-loglevel",
            "error",
        ]
        LOG.info(
            "字幕优先：ffmpeg 抽取为 SRT | map=0:%s file=%s",
            sidx,
            media.name,
            extra={"trace_id": trace_id or "-"},
        )
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            LOG.info(
                "字幕优先：ffmpeg 执行异常，回退 ASR | %s | file=%s",
                e,
                media.name,
                extra={"trace_id": trace_id or "-"},
            )
            return None
        if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            LOG.info(
                "字幕优先：ffmpeg 无 SRT 输出 returncode=%s stderr=%s | file=%s",
                r.returncode,
                (r.stderr or "").strip()[:400],
                media.name,
                extra={"trace_id": trace_id or "-"},
            )
            return None
        raw = out.read_text(encoding="utf-8", errors="replace")
        LOG.info(
            "字幕优先：已得到 SRT 原始长度=%d 字符 | file=%s",
            len(raw or ""),
            media.name,
            extra={"trace_id": trace_id or "-"},
        )
        return raw


async def maybe_punctuate_chinese_text(text: str, qwen_api_key: str, trace_id: str) -> str:
    if not (text or "").strip() or not (qwen_api_key or "").strip():
        return text
    if not _needs_punctuation_guess(text):
        return text
    from dashscope import Generation

    prompt = (
        "为下列无标点或极少标点的连续中文文本补全断句和标点，"
        "只输出修正后的纯文本，不要解释或前后缀：\n\n"
    )
    user_content = f"{prompt}{text[:12000]}"
    LOG.info(
        "字幕文本标点偏少，尝试通义千问断句",
        extra={"trace_id": trace_id or "-"},
    )

    def _call():
        return Generation.call(
            model="qwen-turbo",
            messages=[{"role": "user", "content": user_content}],
            api_key=qwen_api_key.strip(),
            result_format="message",
        )

    try:
        rsp = await asyncio.to_thread(_call)
    except Exception as e:
        LOG.warning(
            "标点补全调用失败: %s",
            e,
            extra={"trace_id": trace_id or "-"},
        )
        return text
    if getattr(rsp, "status_code", None) != 200:
        return text
    out = ""
    output = getattr(rsp, "output", None)
    if output is not None:
        tx = getattr(output, "text", None)
        if tx:
            out = str(tx).strip()
    if not out or len(out) < 4:
        return text
    return out


async def try_extract_embedded_subtitle_plain(
    media_path: Path,
    *,
    qwen_api_key: str,
    trace_id: str,
) -> Optional[str]:
    """
    从媒体文件解出内嵌文字幕为纯文本；无轨或失败返回 None。
    """
    p = media_path.resolve()
    if not p.is_file():
        LOG.info(
            "字幕优先：媒体路径不是文件，跳过字幕提取 | path=%s",
            media_path,
            extra={"trace_id": trace_id or "-"},
        )
        return None
    raw = await asyncio.to_thread(_extract_srt_to_temp, p, trace_id=trace_id)
    if not raw or not raw.strip():
        LOG.info(
            "字幕优先：未得到 SRT 内容，回退 ASR | file=%s",
            p.name,
            extra={"trace_id": trace_id or "-"},
        )
        return None
    plain = srt_to_plain_text(raw)
    if not plain or len(plain.strip()) < 2:
        LOG.info(
            "字幕优先：SRT 转纯文本后过短或为空，回退 ASR | plain_len=%s",
            len(plain or ""),
            extra={"trace_id": trace_id or "-"},
        )
        return None
    plain2 = await maybe_punctuate_chinese_text(plain, qwen_api_key, trace_id)
    out = (plain2 or plain).strip()
    LOG.info(
        "字幕优先：内嵌字幕提取成功，纯文本约 %d 字 | file=%s",
        len(out),
        p.name,
        extra={"trace_id": trace_id or "-"},
    )
    return out
