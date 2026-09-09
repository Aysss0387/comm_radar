"""Persistent Neon storage for the Flask research workbench."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

import asyncpg


class DatabaseUnavailable(RuntimeError):
    pass


class ResearchDatabase:
    """Run asyncpg on one long-lived event loop behind synchronous Flask routes."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self.dsn = (dsn if dsn is not None else os.getenv("DATABASE_URL", "")).strip()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[asyncpg.Pool] = None
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        if self.dsn:
            self._thread = threading.Thread(target=self._serve, name="research-database", daemon=True)
            self._thread.start()
            self._ready.wait(timeout=15)

    @property
    def available(self) -> bool:
        return bool(self.dsn and self._pool and not self._startup_error)

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._pool = self._loop.run_until_complete(
                asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=3, command_timeout=30)
            )
        except BaseException as error:
            self._startup_error = error
        finally:
            self._ready.set()
        if self._pool:
            self._loop.run_forever()

    def _run(self, coroutine: Any) -> Any:
        if not self.available or not self._loop:
            if hasattr(coroutine, "close"):
                coroutine.close()
            raise DatabaseUnavailable(str(self._startup_error or "DATABASE_URL 未配置"))
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=45)

    @staticmethod
    def _json(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    @classmethod
    def _record(cls, row: Mapping[str, Any]) -> Dict[str, Any]:
        paper = dict(cls._json(row["paper"]) or {})
        paper["date"] = row["recommendation_date"].isoformat()
        paper["slot"] = row["slot"]
        paper["feedback"] = {
            "read": bool(row["read"]),
            "starred": bool(row["saved"]),
            "useful": row["helpful"],
        }
        return paper

    async def _fetch_records(self, day: Optional[str] = None) -> List[Dict[str, Any]]:
        assert self._pool
        args: List[Any] = []
        where = ""
        if day:
            where = "WHERE r.recommendation_date = $1::text::date"
            args.append(day)
        rows = await self._pool.fetch(
            f"""SELECT r.recommendation_date, r.slot, r.paper,
                       COALESCE(f.read, false) AS read,
                       COALESCE(f.saved, false) AS saved,
                       f.helpful
                FROM daily_recommendations r
                LEFT JOIN daily_feedback f
                  ON f.recommendation_date = r.recommendation_date
                 AND f.paper_id = r.paper_id
                {where}
                ORDER BY r.recommendation_date DESC,
                         CASE r.slot WHEN 'relevance' THEN 1 WHEN 'theory' THEN 2 ELSE 3 END""",
            *args,
        )
        return [self._record(row) for row in rows]

    def day(self, day: Optional[str] = None) -> Dict[str, Any]:
        async def query() -> Dict[str, Any]:
            assert self._pool
            dates = [row["day"].isoformat() for row in await self._pool.fetch(
                "SELECT DISTINCT recommendation_date AS day FROM daily_recommendations ORDER BY day DESC"
            )]
            target = day or datetime.now(timezone.utc).date().isoformat()
            return {"date": target, "dates": dates, "papers": await self._fetch_records(target)}

        return self._run(query())

    def history(self) -> List[Dict[str, Any]]:
        return self._run(self._fetch_records())

    def excluded_paper_ids(self) -> set[str]:
        async def query() -> set[str]:
            assert self._pool
            rows = await self._pool.fetch("SELECT DISTINCT paper_id FROM daily_recommendations")
            return {str(row["paper_id"]) for row in rows}

        return self._run(query())

    def save_recommendations(self, day: str, records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        async def insert() -> List[Dict[str, Any]]:
            assert self._pool
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    for record in records:
                        await connection.execute(
                            """INSERT INTO daily_recommendations
                               (recommendation_date, slot, paper_id, paper, explanation)
                               VALUES ($1::text::date, $2, $3, $4::jsonb, $5::jsonb)
                               ON CONFLICT (recommendation_date, slot) DO NOTHING""",
                            day,
                            str(record["slot"]),
                            str(record["dedupe_key"]),
                            json.dumps(dict(record), ensure_ascii=False),
                            json.dumps(record.get("analysis"), ensure_ascii=False),
                        )
            return await self._fetch_records(day)

        return self._run(insert())

    def apply_feedback(self, day: str, paper_id: str, field: str, value: Any) -> Dict[str, Any]:
        columns = {"read": "read", "starred": "saved", "useful": "helpful"}
        if field not in columns:
            raise ValueError("反馈字段必须是 read、starred 或 useful。")
        normalized = value if field == "useful" and value in {True, False} else (None if field == "useful" else bool(value))

        async def update() -> Dict[str, Any]:
            assert self._pool
            exists = await self._pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM daily_recommendations WHERE recommendation_date = $1::text::date AND paper_id = $2)",
                day,
                paper_id,
            )
            if not exists:
                raise KeyError(paper_id)
            column = columns[field]
            await self._pool.execute(
                f"""INSERT INTO daily_feedback (recommendation_date, paper_id, {column})
                    VALUES ($1::text::date, $2, $3)
                    ON CONFLICT (recommendation_date, paper_id)
                    DO UPDATE SET {column} = EXCLUDED.{column}, updated_at = now()""",
                day,
                paper_id,
                normalized,
            )
            records = await self._fetch_records(day)
            return next(record for record in records if record.get("dedupe_key") == paper_id)

        return self._run(update())

    def reading_cards(self, collection: Optional[str] = None) -> List[Dict[str, Any]]:
        async def query() -> List[Dict[str, Any]]:
            assert self._pool
            if collection:
                rows = await self._pool.fetch(
                    "SELECT zotero_item_key, metadata, content FROM reading_cards WHERE collection_name = $1 ORDER BY generated_at DESC",
                    collection,
                )
            else:
                rows = await self._pool.fetch(
                    "SELECT zotero_item_key, metadata, content FROM reading_cards ORDER BY generated_at DESC"
                )
            return [{"item_key": row["zotero_item_key"], "metadata": self._json(row["metadata"]), "content": row["content"]} for row in rows]

        return self._run(query())

    def reading_card(self, citekey: str, collection: Optional[str] = None) -> Optional[Dict[str, Any]]:
        async def query() -> Optional[Dict[str, Any]]:
            assert self._pool
            if collection:
                row = await self._pool.fetchrow(
                    "SELECT zotero_item_key, metadata, content FROM reading_cards WHERE citekey = $1 AND collection_name = $2",
                    citekey,
                    collection,
                )
            else:
                row = await self._pool.fetchrow(
                    "SELECT zotero_item_key, metadata, content FROM reading_cards WHERE citekey = $1",
                    citekey,
                )
            return None if not row else {"item_key": row["zotero_item_key"], "metadata": self._json(row["metadata"]), "content": row["content"]}

        return self._run(query())

    def update_reading_card_metadata(self, item_key: str, metadata: Mapping[str, Any]) -> None:
        async def update() -> None:
            assert self._pool
            await self._pool.execute(
                "UPDATE reading_cards SET metadata = $2::jsonb, generated_at = now() WHERE zotero_item_key = $1",
                item_key,
                json.dumps(dict(metadata), ensure_ascii=False),
            )

        self._run(update())

    def save_reading_card(
        self,
        item_key: str,
        collection: str,
        citekey: str,
        metadata: Mapping[str, Any],
        content: str,
        attachment_key: str,
        source_md5: str,
    ) -> None:
        async def save() -> None:
            assert self._pool
            await self._pool.execute(
                """INSERT INTO reading_cards
                   (zotero_item_key, collection_name, citekey, metadata, content, source_attachment_key, source_md5)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                   ON CONFLICT (zotero_item_key) DO UPDATE SET
                     collection_name = EXCLUDED.collection_name,
                     citekey = EXCLUDED.citekey,
                     metadata = EXCLUDED.metadata,
                     content = EXCLUDED.content,
                     source_attachment_key = EXCLUDED.source_attachment_key,
                     source_md5 = EXCLUDED.source_md5,
                     generated_at = now()""",
                item_key,
                collection,
                citekey,
                json.dumps(dict(metadata), ensure_ascii=False),
                content,
                attachment_key,
                source_md5,
            )

        self._run(save())
