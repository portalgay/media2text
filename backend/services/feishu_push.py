# -*- coding: utf-8 -*-
"""飞书多维表格：integration 字段集 + record_uuid upsert"""

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from core import redis_client as R
from core.logger import get_logger, log_error, log_timing
from models.schemas import ProcessConfig
from services.integration_payload import row_datetime_to_ms_utc

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

LOG = get_logger("feishu_push")

_token_cache: Dict[str, Any] = {
    "cred": "",
    "token": "",
    "exp": 0.0,
}


def _cred_key(app_id: str, app_secret: str) -> str:
    return f"{app_id.strip()}|{app_secret.strip()}"


def _peek(s: Optional[str], head: int = 6, tail: int = 4) -> str:
    if not s:
        return "(空)"
    s = str(s).strip()
    if len(s) <= head + tail + 3:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _summarize_feishu_error(body_text: str) -> str:
    raw = (body_text or "").strip()
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:1200]
    parts = []
    if isinstance(j, dict):
        if j.get("code") is not None:
            parts.append(f"code={j.get('code')}")
        if j.get("msg"):
            parts.append(f"msg={j.get('msg')}")
        if j.get("error"):
            parts.append(f"error={j.get('error')}")
        if isinstance(j.get("data"), dict) and j["data"]:
            parts.append(f"data={json.dumps(j['data'], ensure_ascii=False)[:300]}")
    return " | ".join(parts) if parts else raw[:1200]


FEISHU_403_HINT = (
    "403 多为权限问题：① 开放平台应用需开通并发布「多维表格」相关权限；"
    "② 在目标多维表格中将该自建应用添加为具备编辑权限的协作者。"
)


# 列误设为「多选」时，飞书会把以 [ 开头的字符串按 JSON 数组解析，导致 1254063 MultiSelectFieldConvFail
_ZWSP = "\u200b"


def _feishu_source_files_as_text(val: Any) -> str:
    if isinstance(val, (dict, list)):
        s = json.dumps(val, ensure_ascii=False)
    else:
        s = str(val or "")
    if s.lstrip().startswith("["):
        return _ZWSP + s
    return s


def feishu_fields_from_integration(d: Dict[str, Any]) -> Dict[str, Any]:
    """多维表格列类型需与控制台一致；日期时间列通常为毫秒时间戳。"""
    return {
        "record_uuid": str(d.get("record_uuid") or ""),
        "created_at": row_datetime_to_ms_utc(d.get("created_at")),
        "updated_at": row_datetime_to_ms_utc(d.get("updated_at")),
        "title": str(d.get("title") or ""),
        "category": str(d.get("category") or ""),
        "batch_mode": str(d.get("batch_mode") or ""),
        "captions": str(d.get("captions") or ""),
        "summary": str(d.get("summary") or ""),
        "source_files": _feishu_source_files_as_text(d.get("source_files")),
    }


async def _tenant_token(app_id: str, app_secret: str, trace_id: str) -> str:
    cred = _cred_key(app_id, app_secret)
    now = time.time()
    if (
        _token_cache["cred"] == cred
        and _token_cache["token"]
        and isinstance(_token_cache["exp"], (int, float))
        and now < float(_token_cache["exp"]) - 120
    ):
        LOG.info(
            "飞书 tenant_access_token 使用缓存 | app_id=%s",
            _peek(app_id),
            extra={"trace_id": trace_id},
        )
        return str(_token_cache["token"])

    LOG.info(
        "飞书 请求 tenant_access_token | app_id=%s",
        _peek(app_id),
        extra={"trace_id": trace_id},
    )
    t0 = time.time()
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            _TOKEN_URL,
            json={"app_id": app_id.strip(), "app_secret": app_secret.strip()},
        )
    log_timing(LOG, trace_id, "feishu_tenant_token", time.time() - t0)
    body_preview = _summarize_feishu_error(r.text)
    if r.status_code >= 400:
        LOG.error(
            "飞书 tenant_token HTTP 失败 | status=%s | %s",
            r.status_code,
            body_preview,
            extra={"trace_id": trace_id},
        )
        raise RuntimeError(
            f"飞书获取 tenant_access_token 失败 HTTP {r.status_code}: {body_preview}"
        )
    try:
        data = r.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"飞书 tenant_token 响应异常: {r.text[:500]}") from None

    if data.get("code") != 0:
        raise RuntimeError(f"飞书授权失败: {data}")

    tok = data.get("tenant_access_token") or ""
    if not tok:
        raise RuntimeError("飞书返回中无 tenant_access_token")
    exp = int(data.get("expire", 7200))
    _token_cache["cred"] = cred
    _token_cache["token"] = tok
    _token_cache["exp"] = now + exp
    LOG.info(
        "飞书 tenant_access_token 获取成功 | token_len=%s",
        len(tok),
        extra={"trace_id": trace_id},
    )
    return tok


async def _search_record_id(
    client: httpx.AsyncClient,
    *,
    app_token: str,
    table_id: str,
    record_uuid: str,
    tenant_token: str,
    trace_id: str,
) -> Optional[str]:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/"
        f"{table_id}/records/search"
    )
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    body = {
        "automatic_fields": False,
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": "record_uuid",
                    "operator": "is",
                    "value": [record_uuid],
                }
            ],
        },
    }
    t0 = time.time()
    r = await client.post(url, headers=headers, json=body)
    log_timing(LOG, trace_id, "feishu_search_record", time.time() - t0)
    if r.status_code >= 400:
        log_error(LOG, trace_id, "feishu_search", Exception(r.text[:400]), status=r.status_code)
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    if data.get("code") != 0:
        return None
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return None
    return str(items[0].get("record_id") or "")


def _record_id_from_batch_create(data: Dict[str, Any]) -> Optional[str]:
    recs = (data.get("data") or {}).get("records") or []
    if not recs:
        return None
    rid = str((recs[0] or {}).get("record_id") or "").strip()
    return rid or None


async def push_items_to_feishu(
    items: List[Dict[str, Any]],
    cfg: ProcessConfig,
    *,
    trace_id: str = "-",
) -> None:
    app_id = (cfg.feishu_app_id or "").strip()
    sec = (cfg.feishu_app_secret or "").strip()
    app_token = (cfg.feishu_bitable_app_token or "").strip()
    table_id = (cfg.feishu_table_id or "").strip()
    if not all([app_id, sec, app_token, table_id]):
        raise ValueError("请在设置中填写飞书 App ID、App Secret、多维表格 App Token、数据表 Table ID")

    LOG.info(
        "飞书推送开始 | 条数=%s | table_id=%s",
        len(items),
        table_id,
        extra={"trace_id": trace_id},
    )

    tenant_token = await _tenant_token(app_id, sec, trace_id)
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        for it in items:
            fields = feishu_fields_from_integration(dict(it))
            ru = str(it.get("record_uuid") or "").strip()
            db_row_id = it.get("record_id")
            existing_id: Optional[str] = None
            if ru:
                try:
                    cached = await R.push_cache_get(ru)
                except Exception as ex:
                    cached = None
                    log_error(LOG, trace_id, "push_cache_get_feishu", ex)
                if cached:
                    cid = str(cached.get("feishu_record_id") or "").strip()
                    if cid:
                        existing_id = cid
                if not existing_id:
                    existing_id = await _search_record_id(
                        client,
                        app_token=app_token,
                        table_id=table_id,
                        record_uuid=ru,
                        tenant_token=tenant_token,
                        trace_id=trace_id,
                    )

            if existing_id:
                up_url = (
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/"
                    f"{table_id}/records/{existing_id}"
                )
                t0 = time.time()
                r = await client.put(up_url, headers=headers, json={"fields": fields})
                log_timing(LOG, trace_id, "feishu_record_put", time.time() - t0)
                final_record_id: Optional[str] = existing_id
            else:
                cr_url = (
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/"
                    f"{table_id}/records/batch_create"
                )
                t0 = time.time()
                r = await client.post(
                    cr_url, headers=headers, json={"records": [{"fields": fields}]}
                )
                log_timing(LOG, trace_id, "feishu_batch_create", time.time() - t0)
                final_record_id = None

            body_summary = _summarize_feishu_error(r.text)
            if r.status_code >= 400:
                LOG.error(
                    "飞书写入 HTTP 失败 | status=%s | %s",
                    r.status_code,
                    r.text[:1200],
                    extra={"trace_id": trace_id},
                )
                msg = f"飞书 HTTP {r.status_code}: {body_summary}"
                if r.status_code == 403:
                    msg = f"{msg}。{FEISHU_403_HINT}"
                raise RuntimeError(msg)
            try:
                data = r.json()
            except json.JSONDecodeError:
                raise RuntimeError(f"飞书响应非 JSON: {r.text[:500]}") from None
            if data.get("code") != 0:
                raise RuntimeError(f"飞书写入失败: {data}")

            if not final_record_id:
                final_record_id = _record_id_from_batch_create(data)

            if ru and final_record_id:
                merge: Dict[str, Any] = {"feishu_record_id": final_record_id}
                if db_row_id is not None:
                    merge["db_id"] = db_row_id
                try:
                    await R.push_cache_set_merged(ru, merge)
                except Exception as ex:
                    log_error(LOG, trace_id, "push_cache_set_feishu", ex)
