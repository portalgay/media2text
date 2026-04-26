# -*- coding: utf-8 -*-
"""历史记录补操作：总结、字幕上传 OSS、单条推送、补转写"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

from core import redis_client as R
from core.config_loader import BACKEND_ROOT, settings
from core.logger import get_logger
from models.schemas import DEFAULT_CONFIG_DICT, ProcessConfig, process_config_from_merged
from services import asr_dashscope, asr_local, db
from services.batch_mode_util import normalize_batch_mode_db
from services.feishu_push import push_items_to_feishu
from services.integration_payload import integration_dict_from_row
from services.markdown_formatter import format_merged, format_single
from services.notion_push import push_items_to_notion
from services.file_hash import sha256_bytes_prefix8, sha256_file_prefix8
from services.filename_clean import safe_display_stem
from services.oss_uploader import upload_caption_to_oss, upload_to_oss
from services.summarizer import get_summarizer
from services.video_converter import audio_to_mp3_if_needed, probe_media_duration_seconds

LOG = get_logger("history_ops")


def _temp_root() -> Path:
    td = settings.temp_dir
    if not td.is_absolute():
        return (BACKEND_ROOT / td).resolve()
    return td.resolve()


def _row_needs_transcribe(row: Dict[str, Any]) -> bool:
    rt = str(row.get("recognition_type") or "").strip()
    return rt in ("", "untranscribed", "unrecognized")


def _merge_recognition_types(types: List[str]) -> str:
    if not types:
        return "untranscribed"
    if all(t == "subtitle" for t in types):
        return "subtitle"
    if any(t == "dashscope" for t in types):
        return "dashscope"
    if all(t == "untranscribed" for t in types):
        return "untranscribed"
    return "funasr"


async def _download_url_to_temp(url: str, trace_id: str) -> Path:
    tr = _temp_root()
    tr.mkdir(parents=True, exist_ok=True)
    dest = tr / f"his_{trace_id}_{uuid.uuid4().hex[:10]}.bin"
    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True, trust_env=False) as client:
        r = await client.get(url)
        if r.status_code >= 400:
            raise ValueError(f"下载音频失败 HTTP {r.status_code}")
        dest.write_bytes(r.content)
    return dest


async def _resolve_work_audio_mp3(
    sf: Dict[str, Any], cfg: ProcessConfig, trace_id: str
) -> Tuple[Path, str, str]:
    fname = str(sf.get("filename") or sf.get("original_filename") or "audio.mp3")
    stem_safe = safe_display_stem(Path(fname).stem)
    local = (sf.get("audio_local_path") or "").strip()
    if local:
        p = Path(local)
        if p.is_file():
            h8 = await asyncio.to_thread(sha256_file_prefix8, p)
            out_mp3 = _temp_root() / f"{trace_id}_{h8[:8]}_work.mp3"
            work = await audio_to_mp3_if_needed(p, out_mp3)
            return work, h8, stem_safe
    url = (sf.get("audio_oss_url") or "").strip()
    if not url:
        raise ValueError(
            "无本地音频且缺少音频 OSS 链接，无法补转写。请在处理时开启「保存音频到本地」或「上传音频到 OSS」。"
        )
    raw = await _download_url_to_temp(url, trace_id)
    h8 = await asyncio.to_thread(sha256_file_prefix8, raw)
    out_mp3 = _temp_root() / f"{trace_id}_{h8[:8]}_dl.mp3"
    try:
        work = await audio_to_mp3_if_needed(raw, out_mp3)
        return work, h8, stem_safe
    finally:
        try:
            raw.unlink(missing_ok=True)
        except OSError:
            pass


async def _transcribe_work_mp3(
    work_mp3: Path,
    cfg: ProcessConfig,
    trace_id: str,
    *,
    audio_oss_url: str,
    stem_safe: str,
    h8: str,
) -> Tuple[str, str]:
    if cfg.asr_engine == "funasr":
        text = await asr_local.transcribe(work_mp3, trace_id)
        return text or "", "funasr"
    oss_url = (audio_oss_url or "").strip()
    if not oss_url:
        oss_url = await upload_to_oss(work_mp3, h8, stem_safe, "audios", cfg)
    try:
        audio_sz = work_mp3.stat().st_size
    except OSError:
        audio_sz = None
    audio_dur = await probe_media_duration_seconds(work_mp3)
    text = await asr_dashscope.transcribe(
        oss_url,
        cfg.dashscope_api_key,
        trace_id,
        audio_duration_sec=audio_dur,
        audio_bytes=audio_sz,
    )
    return text or "", "dashscope"


def _safe_unlink(p: Path) -> None:
    try:
        if p.is_file():
            p.unlink(missing_ok=True)
    except OSError:
        pass


async def _merged_cfg() -> ProcessConfig:
    merged = dict(DEFAULT_CONFIG_DICT)
    merged.update(await R.get_config())
    return process_config_from_merged(merged)


async def transcribe_record(record_id: int) -> Dict[str, Any]:
    """对「未转写」记录按当前配置补跑 ASR，写入 captions 与 recognition_type。"""
    row = await db.get_record(record_id)
    if not row:
        raise ValueError("记录不存在")
    if not _row_needs_transcribe(row):
        raise ValueError("当前记录已转写，请使用重新处理流程如需更换引擎")

    cfg = await _merged_cfg()
    if cfg.asr_engine == "dashscope":
        if not (cfg.dashscope_api_key or "").strip():
            raise ValueError("百炼转写需要配置 DashScope API Key")
        need = [
            cfg.oss_access_key_id,
            cfg.oss_access_key_secret,
            cfg.oss_bucket_name,
            cfg.oss_endpoint,
        ]
        if not all((x or "").strip() for x in need):
            raise ValueError("百炼转写需要完整 OSS 配置")
    elif cfg.asr_engine != "funasr":
        raise ValueError(f"不支持的转写引擎: {cfg.asr_engine}")

    sfs: List[Any] = list(row.get("source_files") or [])
    if not sfs:
        raise ValueError("记录中无来源文件信息，无法补转写")

    bm = normalize_batch_mode_db(row.get("batch_mode"))
    trace_id = f"his{record_id}"

    if bm == "批数据":
        merge_pairs: List[Tuple[str, str]] = []
        merge_recs: List[str] = []
        for sf in sfs:
            if not isinstance(sf, dict):
                continue
            fn = str(sf.get("filename") or sf.get("original_filename") or "clip")
            work_mp3, h8, stem_safe = await _resolve_work_audio_mp3(sf, cfg, trace_id)
            try:
                au_url = (sf.get("audio_oss_url") or "").strip()
                transcript, rt = await _transcribe_work_mp3(
                    work_mp3,
                    cfg,
                    trace_id,
                    audio_oss_url=au_url,
                    stem_safe=stem_safe,
                    h8=h8,
                )
            finally:
                _safe_unlink(work_mp3)
            if not (transcript or "").strip():
                continue
            merge_pairs.append((fn, transcript))
            merge_recs.append(rt)
        if not merge_pairs:
            raise ValueError("未能从任何文件得到转写文本，请检查音频与引擎配置")
        markdown = format_merged(merge_pairs)
        rec_merged = _merge_recognition_types(merge_recs)
    else:
        sf0 = sfs[0]
        if not isinstance(sf0, dict):
            raise ValueError("记录来源文件结构异常")
        fn = str(sf0.get("filename") or sf0.get("original_filename") or "clip")
        work_mp3, h8, stem_safe = await _resolve_work_audio_mp3(sf0, cfg, trace_id)
        try:
            au_url = (sf0.get("audio_oss_url") or "").strip()
            transcript, rec_merged = await _transcribe_work_mp3(
                work_mp3,
                cfg,
                trace_id,
                audio_oss_url=au_url,
                stem_safe=stem_safe,
                h8=h8,
            )
        finally:
            _safe_unlink(work_mp3)
        if not (transcript or "").strip():
            raise ValueError("转写结果为空")
        markdown = format_single(fn, transcript)

    await db.update_record_content(
        record_id, captions=markdown, recognition_type=rec_merged
    )
    out = await db.get_record(record_id)
    if not out:
        raise ValueError("记录不存在")
    return out


async def summarize_record(record_id: int) -> Dict[str, Any]:
    row = await db.get_record(record_id)
    if not row:
        raise ValueError("记录不存在")
    caps = (row.get("captions") or "").strip()
    if not caps:
        raise ValueError("无原文可总结")

    cfg = await _merged_cfg()
    prompt = await R.get_prompt_content(cfg.summary_prompt_title)
    summ = get_summarizer(cfg.summary_model, cfg.qwen_api_key)
    text = await summ.summarize(caps, prompt, trace_id=f"his{record_id}")

    await db.update_record_summary(record_id, text, True)
    return await db.get_record(record_id)


async def upload_caption_oss_record(record_id: int) -> Dict[str, Any]:
    row = await db.get_record(record_id)
    if not row:
        raise ValueError("记录不存在")
    caps = row.get("captions")
    if not caps or not str(caps).strip():
        raise ValueError("无字幕文本可上传")

    cfg = await _merged_cfg()
    sfs_src: list = list(row.get("source_files") or [])
    if sfs_src and isinstance(sfs_src[0], dict):
        fn = (sfs_src[0].get("filename") or f"{(row.get('title') or 'caption')}.md") + ""
    else:
        fn = f"{Path((row.get('title') or 'caption').strip() or 'caption').stem}.md"
    stem = safe_display_stem(Path(fn).stem)
    cap_h8 = sha256_bytes_prefix8(str(caps).encode("utf-8"))
    url = await upload_caption_to_oss(str(caps), cap_h8, stem, cfg)

    sfs: list = list(row.get("source_files") or [])
    if sfs and isinstance(sfs, list):
        if isinstance(sfs[0], dict):
            sfs[0] = {**sfs[0], "caption_oss_url": url}
        else:
            sfs = [{"filename": Path(fn).name, "caption_oss_url": url}]
    else:
        sfs = [{"filename": Path(fn).name, "caption_oss_url": url}]

    await db.update_record_caption_oss(record_id, sfs, True)
    return await db.get_record(record_id)


async def push_record_notion(record_id: int) -> Dict[str, Any]:
    row = await db.get_record(record_id)
    if not row:
        raise ValueError("记录不存在")
    cfg = await _merged_cfg()
    payload = integration_dict_from_row(row)
    await push_items_to_notion([payload], cfg, trace_id=f"h{record_id}")
    await db.update_push_flags([record_id], notion=True)
    out = await db.get_record(record_id)
    if not out:
        raise ValueError("记录不存在")
    return out


async def push_record_feishu(record_id: int) -> Dict[str, Any]:
    row = await db.get_record(record_id)
    if not row:
        raise ValueError("记录不存在")
    cfg = await _merged_cfg()
    payload = integration_dict_from_row(row)
    await push_items_to_feishu([payload], cfg, trace_id=f"h{record_id}")
    await db.update_push_flags([record_id], feishu=True)
    out = await db.get_record(record_id)
    if not out:
        raise ValueError("记录不存在")
    return out
