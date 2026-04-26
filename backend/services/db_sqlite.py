# -*- coding: utf-8 -*-
"""SQLite 实现（aiosqlite）"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosqlite

from core.config_loader import settings
from services.batch_mode_util import normalize_batch_mode_db, normalize_batch_mode_display
from services.db_adapter import BaseDBAdapter


def _utc_now_str() -> str:
    """SQLite 与 CURRENT_TIMESTAMP 一致：存 UTC  wall 字符串（无时区后缀，约定为 UTC）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SQLiteAdapter(BaseDBAdapter):
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path or settings.sqlite_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    task_id      TEXT NOT NULL,
                    category     TEXT NOT NULL DEFAULT '默认分类',
                    batch_mode   TEXT NOT NULL DEFAULT '单数据',
                    title        TEXT NOT NULL,
                    source_files TEXT NOT NULL,
                    captions     TEXT,
                    summary      TEXT,
                    status       TEXT DEFAULT 'success',
                    transcript_on_oss INTEGER NOT NULL DEFAULT 0,
                    has_summary INTEGER NOT NULL DEFAULT 0,
                    notion_pushed INTEGER NOT NULL DEFAULT 0,
                    feishu_pushed INTEGER NOT NULL DEFAULT 0,
                    record_uuid  TEXT
                )
                """
            )
            await db.commit()

        async with aiosqlite.connect(self._db_path) as db:
            await self._migrate(db)
            await self._backfill_record_uuids(db)
            await db.commit()

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(records)")
        rows = await cur.fetchall()
        cols = {row[1] for row in rows}
        alters: List[str] = []
        if "updated_at" not in cols:
            alters.append(
                "ALTER TABLE records ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
            )
        if "transcript_on_oss" not in cols:
            alters.append(
                "ALTER TABLE records ADD COLUMN transcript_on_oss INTEGER NOT NULL DEFAULT 0"
            )
        if "has_summary" not in cols:
            alters.append(
                "ALTER TABLE records ADD COLUMN has_summary INTEGER NOT NULL DEFAULT 0"
            )
        if "notion_pushed" not in cols:
            alters.append(
                "ALTER TABLE records ADD COLUMN notion_pushed INTEGER NOT NULL DEFAULT 0"
            )
        if "feishu_pushed" not in cols:
            alters.append(
                "ALTER TABLE records ADD COLUMN feishu_pushed INTEGER NOT NULL DEFAULT 0"
            )
        if "record_uuid" not in cols:
            alters.append("ALTER TABLE records ADD COLUMN record_uuid TEXT")
        if "recognition_type" not in cols:
            alters.append(
                "ALTER TABLE records ADD COLUMN recognition_type TEXT NOT NULL DEFAULT 'funasr'"
            )
        for sql in alters:
            await db.execute(sql)

        await self._migrate_batch_mode_values(db)

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_record_uuid ON records(record_uuid)"
        )

        cur2 = await db.execute("PRAGMA table_info(records)")
        cols2 = {row[1] for row in await cur2.fetchall()}
        if "updated_at" in cols2:
            await db.execute(
                "UPDATE records SET updated_at = COALESCE(updated_at, created_at) WHERE updated_at IS NULL"
            )

    async def _migrate_batch_mode_values(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            UPDATE records SET batch_mode = '单数据'
            WHERE batch_mode IN ('separate', '单数据')
            """
        )
        await db.execute(
            """
            UPDATE records SET batch_mode = '批数据'
            WHERE batch_mode IN ('merge', '批数据')
            """
        )

    async def _backfill_record_uuids(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute(
            "SELECT id FROM records WHERE record_uuid IS NULL OR record_uuid = ''"
        )
        rows = await cur.fetchall()
        for row in rows:
            rid = int(row[0])
            await db.execute(
                "UPDATE records SET record_uuid = ? WHERE id = ?",
                (uuid.uuid4().hex, rid),
            )

    REC_FIELDS = (
        "id, created_at, updated_at, task_id, category, batch_mode, title, source_files, "
        "captions, summary, status, transcript_on_oss, has_summary, notion_pushed, feishu_pushed, record_uuid, "
        "recognition_type"
    )

    def _row_to_item(self, d: dict) -> dict:
        try:
            d["source_files"] = json.loads(d["source_files"])
        except Exception:
            d["source_files"] = []
        for k in ("transcript_on_oss", "has_summary", "notion_pushed", "feishu_pushed"):
            if k in d:
                d[k] = bool(d[k])
        d["transcript_uploaded_oss"] = d.get("transcript_on_oss", False)
        d["summarized"] = d.get("has_summary", False)
        d["pushed_notion"] = d.get("notion_pushed", False)
        d["pushed_feishu"] = d.get("feishu_pushed", False)
        rt = d.get("recognition_type")
        rs = (str(rt).strip() if rt is not None else "") or "untranscribed"
        if rs == "unrecognized":
            rs = "untranscribed"
        d["recognition_type"] = rs
        bm = d.get("batch_mode")
        d["batch_mode_display"] = normalize_batch_mode_display(bm)
        return d

    def _norm_recognition_type(self, record: Dict[str, Any]) -> str:
        rt = record.get("recognition_type")
        if rt is None:
            return "untranscribed"
        s = str(rt).strip()[:32]
        if not s:
            return "untranscribed"
        if s == "unrecognized":
            return "untranscribed"
        return s

    async def save_record(self, record: Dict[str, Any]) -> Tuple[int, str]:
        ru = (record.get("record_uuid") or "").strip() or uuid.uuid4().hex
        bm = normalize_batch_mode_db(record.get("batch_mode"))
        rts = self._norm_recognition_type(record)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id FROM records WHERE record_uuid = ? LIMIT 1", (ru,)
            )
            existing = await cur.fetchone()
            uts = _utc_now_str()
            if existing:
                rid = int(existing[0])
                await db.execute(
                    """
                    UPDATE records SET
                        task_id = ?, category = ?, batch_mode = ?, title = ?, source_files = ?,
                        captions = ?, summary = ?, status = ?,
                        transcript_on_oss = ?, has_summary = ?,
                        record_uuid = ?, recognition_type = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        record.get("task_id", ""),
                        record.get("category", "默认分类"),
                        bm,
                        record.get("title", ""),
                        json.dumps(record.get("source_files", []), ensure_ascii=False),
                        record.get("captions"),
                        record.get("summary"),
                        record.get("status", "success"),
                        1 if record.get("transcript_on_oss") else 0,
                        1 if record.get("has_summary") else 0,
                        ru,
                        rts,
                        uts,
                        rid,
                    ),
                )
                await db.commit()
                return rid, ru

            cur2 = await db.execute(
                """
                INSERT INTO records (
                    task_id, category, batch_mode, title, source_files, captions, summary, status,
                    transcript_on_oss, has_summary, notion_pushed, feishu_pushed, record_uuid, recognition_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("task_id", ""),
                    record.get("category", "默认分类"),
                    bm,
                    record.get("title", ""),
                    json.dumps(record.get("source_files", []), ensure_ascii=False),
                    record.get("captions"),
                    record.get("summary"),
                    record.get("status", "success"),
                    1 if record.get("transcript_on_oss") else 0,
                    1 if record.get("has_summary") else 0,
                    0,
                    0,
                    ru,
                    rts,
                ),
            )
            await db.commit()
            rid = int(cur2.lastrowid)
            return rid, ru

    async def update_record_summary(self, record_id: int, summary: str, has_summary: bool = True) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE records SET summary = ?, has_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (summary, 1 if has_summary else 0, _utc_now_str(), record_id),
            )
            await db.commit()

    async def update_record_caption_oss(
        self,
        record_id: int,
        source_files: List[dict],
        transcript_on_oss: bool = True,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE records SET source_files = ?, transcript_on_oss = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(source_files, ensure_ascii=False),
                    1 if transcript_on_oss else 0,
                    _utc_now_str(),
                    record_id,
                ),
            )
            await db.commit()

    async def update_record_content(
        self,
        record_id: int,
        *,
        captions: Optional[str] = None,
        summary: Optional[str] = None,
        recognition_type: Optional[str] = None,
    ) -> None:
        if captions is None and summary is None and recognition_type is None:
            return
        sets: List[str] = []
        vals: List[Any] = []
        if captions is not None:
            sets.append("captions = ?")
            vals.append(captions)
        if summary is not None:
            sets.append("summary = ?")
            vals.append(summary)
            sets.append("has_summary = ?")
            vals.append(1 if str(summary).strip() else 0)
        if recognition_type is not None:
            sets.append("recognition_type = ?")
            vals.append(recognition_type)
        sets.append("updated_at = ?")
        vals.append(_utc_now_str())
        vals.append(record_id)
        sql = f"UPDATE records SET {', '.join(sets)} WHERE id = ?"
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(sql, vals)
            await db.commit()

    async def get_records(self, page: int = 1, page_size: int = 20) -> dict:
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        offset = (page - 1) * page_size
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT COUNT(*) FROM records")
            row = await cur.fetchone()
            total = int(row[0]) if row else 0
            cur = await db.execute(
                f"""
                SELECT {self.REC_FIELDS}
                FROM records
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            rows = await cur.fetchall()
            items = [self._row_to_item(dict(r)) for r in rows]
            return {"total": total, "items": items}

    async def get_record(self, record_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT {self.REC_FIELDS} FROM records WHERE id = ?", (record_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            return self._row_to_item(dict(row))

    async def fetch_records_for_export(self, ids: Optional[Sequence[int]]) -> List[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if ids is None or len(ids) == 0:
                cur = await db.execute(
                    f"SELECT {self.REC_FIELDS} FROM records ORDER BY id ASC"
                )
            else:
                clean = []
                for x in ids:
                    try:
                        xi = int(x)
                        if xi > 0:
                            clean.append(xi)
                    except (TypeError, ValueError):
                        continue
                if not clean:
                    return []
                ph = ",".join("?" * len(clean))
                cur = await db.execute(
                    f"SELECT {self.REC_FIELDS} FROM records WHERE id IN ({ph}) ORDER BY id ASC",
                    clean,
                )
            rows = await cur.fetchall()
            return [self._row_to_item(dict(r)) for r in rows]

    async def update_push_flags(
        self,
        record_ids: Sequence[int],
        *,
        notion: bool = False,
        feishu: bool = False,
    ) -> None:
        clean: List[int] = []
        for x in record_ids:
            try:
                xi = int(x)
                if xi > 0:
                    clean.append(xi)
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        placeholders = ",".join("?" * len(clean))
        async with aiosqlite.connect(self._db_path) as db:
            uts = _utc_now_str()
            if notion:
                await db.execute(
                    f"UPDATE records SET notion_pushed = 1, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [uts] + clean,
                )
            if feishu:
                await db.execute(
                    f"UPDATE records SET feishu_pushed = 1, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [uts] + clean,
                )
            await db.commit()

    def _clean_positive_ids(self, ids: Sequence[int]) -> List[int]:
        clean: List[int] = []
        for x in ids:
            try:
                xi = int(x)
                if xi > 0:
                    clean.append(xi)
            except (TypeError, ValueError):
                continue
        return clean

    async def fetch_record_uuids_by_ids(self, ids: Sequence[int]) -> List[str]:
        clean = self._clean_positive_ids(ids)
        if not clean:
            return []
        placeholders = ",".join("?" * len(clean))
        q = (
            f"SELECT DISTINCT record_uuid FROM records WHERE id IN ({placeholders}) "
            "AND record_uuid IS NOT NULL AND TRIM(record_uuid) != ''"
        )
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(q, clean)
            rows = await cur.fetchall()
        out = {str(r[0]).strip() for r in rows if r and r[0]}
        return sorted(out)

    async def fetch_all_record_uuids(self) -> List[str]:
        q = (
            "SELECT DISTINCT record_uuid FROM records "
            "WHERE record_uuid IS NOT NULL AND TRIM(record_uuid) != ''"
        )
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(q)
            rows = await cur.fetchall()
        out = {str(r[0]).strip() for r in rows if r and r[0]}
        return sorted(out)

    async def delete_record(self, record_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM records WHERE id = ?", (record_id,))
            await db.commit()

    async def delete_records(self, ids: Sequence[int]) -> int:
        clean = self._clean_positive_ids(ids)
        if not clean:
            return 0
        placeholders = ",".join("?" * len(clean))
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(f"DELETE FROM records WHERE id IN ({placeholders})", clean)
            await db.commit()
            return int(cur.rowcount or 0)

    async def clear_all_records(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM records")
            await db.commit()
