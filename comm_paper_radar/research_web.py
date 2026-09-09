"""Local web workbench for Markdown-first Zotero research cards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import markdown
import requests
from flask import Flask, jsonify, request, send_from_directory

from .research_workspace import (
    DEFAULT_BBT_URL,
    DEFAULT_ZOTERO_URL,
    BetterBibTeXClient,
    ResearchWorkspaceError,
    ZoteroLocalClient,
    bbt_planned_citekey,
    zotero_web_client_from_env,
    extract_my_thoughts,
    iter_collection_cards,
    load_card,
    preserve_my_thoughts,
    render_card_template,
    render_front_matter,
    sync_zotero_notes,
    validate_card,
)
from .research_reading import ReadingMaterialStore, card_document, evidence_ids, reading_prompt


READING_STATUSES = ("待读", "初读", "精读中", "精读完成")
HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)
EVIDENCE_RE = re.compile(r"\[([^\]]+?\s*(?:¶|段落|paragraph)\s*\d+)\]", re.IGNORECASE)
EVIDENCE_LOCATOR_RE = re.compile(r"(?P<citekey>[^\s\[\]]+)\s*(?:¶|段落|paragraph)\s*(?P<number>\d+)", re.IGNORECASE)


def _published() -> bool:
    """True when serving from Vercel: Zotero reads go to the Web API, not localhost."""
    return bool(os.environ.get("VERCEL"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_sections(body: str) -> Dict[str, str]:
    matches = list(HEADING_RE.finditer(body))
    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[title] = body[start:end].strip()
    return sections


def _safe_id(value: str) -> str:
    return value.strip()


class CardStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.cards_dir = self.base_dir / "research" / "cards"
        self.collections_dir = self.base_dir / "research" / "collections"
        self.history_dir = self.base_dir / "research" / "history" / "cards"
        self.reading_store = ReadingMaterialStore(self.base_dir)
        self._lock = threading.RLock()

    def collections(self) -> List[str]:
        values = {str(meta.get("collection")) for path in self.cards_dir.glob("*.md") for meta in [self._load(path)[0]] if meta.get("collection")}
        try:
            client = zotero_web_client_from_env() if _published() else ZoteroLocalClient()
            values.update(str(item.get("data", {}).get("name")) for item in client.collections() if item.get("data", {}).get("name"))
        except Exception:
            pass
        return sorted(values, key=str.casefold)

    def _load(self, path: Path) -> Tuple[Dict[str, Any], str]:
        return load_card(path)

    def _find(self, citekey: str, collection: Optional[str] = None) -> Tuple[Path, Dict[str, Any], str]:
        for path in self.cards_dir.glob("*.md"):
            try:
                metadata, body = self._load(path)
            except (OSError, ValueError):
                continue
            if str(metadata.get("citekey")) == citekey and (collection is None or metadata.get("collection") == collection):
                return path, metadata, body
        raise KeyError(citekey)

    def _summary(self, path: Path, metadata: Dict[str, Any], body: str) -> Dict[str, Any]:
        citekey = str(metadata.get("citekey", path.stem))
        questions = metadata.get("questions", [])
        if not isinstance(questions, list):
            questions = []
        return {
            "id": citekey,
            "citekey": citekey,
            "title": str(metadata.get("title", path.stem)),
            "collection": str(metadata.get("collection", "")),
            "zotero_item_key": metadata.get("zotero_item_key"),
            "reading_status": metadata.get("reading_status", "初读"),
            "evidence_status": metadata.get("evidence_status", "来源待核对"),
            "reading_type": metadata.get("reading_type", "精读卡"),
            "sync_status": metadata.get("sync_status", "未同步"),
            "zotero_note_key": metadata.get("zotero_note_key"),
            "theories": [str(item.get("name")) for item in metadata.get("theories", []) if isinstance(item, dict) and item.get("name")],
            "methods": [str(item.get("name")) for item in metadata.get("methods", []) if isinstance(item, dict) and item.get("name")],
            "questions": questions,
            "updated_at": metadata.get("updated_at"),
            "path": str(path.relative_to(self.base_dir)),
            "hash": _sha(path.read_text(encoding="utf-8")),
            "has_card": True,
        }

    def list_cards(self, collection: Optional[str] = None, query: str = "", status: Optional[str] = None, theory: Optional[str] = None, method: Optional[str] = None) -> List[Dict[str, Any]]:
        query = query.strip().casefold()
        result: List[Dict[str, Any]] = []
        for path in sorted(self.cards_dir.glob("*.md")):
            try:
                metadata, body = self._load(path)
            except (OSError, ValueError):
                continue
            if collection and metadata.get("collection") != collection:
                continue
            summary = self._summary(path, metadata, body)
            haystack = " ".join([summary["title"], summary["citekey"], body, " ".join(summary["theories"]), " ".join(summary["methods"])]).casefold()
            if query and query not in haystack:
                continue
            if status and summary["reading_status"] != status:
                continue
            if theory and theory not in summary["theories"]:
                continue
            if method and method not in summary["methods"]:
                continue
            result.append(summary)
        if collection:
            result.extend(self._uncarded_zotero_items(collection, query, status, theory, method, {item["zotero_item_key"] for item in result if item.get("has_card") and item.get("zotero_item_key")}))
        return result

    def _uncarded_zotero_items(self, collection: str, query: str, status: Optional[str], theory: Optional[str], method: Optional[str], card_item_keys: set) -> List[Dict[str, Any]]:
        """Show Zotero papers that have no Markdown card yet, without creating one."""
        try:
            zotero = zotero_web_client_from_env() if _published() else ZoteroLocalClient()
            zotero_collection = zotero.collection_by_name(collection)
            items = zotero.collection_items(str(zotero_collection.get("key")))
            keys = [str(item.get("key") or item.get("data", {}).get("key") or "") for item in items]
            citekeys = BetterBibTeXClient().citationkeys(keys) if keys and not _published() else {}
        except Exception:
            return []
        result = []
        for item in items:
            item_key = str(item.get("key") or item.get("data", {}).get("key") or "")
            if not item_key or item_key in card_item_keys:
                continue
            data = item.get("data", {})
            citekey = citekeys.get(item_key) or data.get("citationKey") or bbt_planned_citekey(item) or item_key
            title = str(data.get("title", "未命名条目"))
            if query and query.casefold() not in f"{title} {citekey}".casefold():
                continue
            if status and status != "未建卡":
                continue
            if theory or method:
                continue
            result.append({"id": citekey, "citekey": citekey, "title": title, "collection": collection, "zotero_item_key": item_key, "reading_status": "未建卡", "evidence_status": "尚未建立证据卡", "theories": [], "methods": [], "questions": [], "updated_at": None, "path": None, "hash": None, "has_card": False})
        return result

    def detail(self, citekey: str, collection: Optional[str] = None) -> Dict[str, Any]:
        path, metadata, body = self._find(citekey, collection)
        sections = _split_sections(body)
        evidence = []
        evidence_file = str(metadata.get("evidence_file") or "")
        snapshot = self._load_evidence_snapshot(evidence_file)
        referenced_ids = evidence_ids(body)
        for evidence_id in referenced_ids:
            chunk = next((item for item in snapshot.get("chunks", []) if item.get("id") == evidence_id), None)
            if chunk:
                evidence.append({"id": evidence_id, "text": evidence_id, "status": "已定位", "source": {"status": "retrieved", "message": f"全文快照位置 {chunk['start']}–{chunk['end']}；请对照 PDF 核查。", "text": chunk["text"], "attachment_key": snapshot.get("attachment_key"), "attachment_name": snapshot.get("attachment_key")}})
        for index, match in enumerate(EVIDENCE_RE.finditer(body), start=1):
            text = match.group(1)
            locator = EVIDENCE_LOCATOR_RE.search(text)
            source = self._source_excerpt(metadata, locator.group("number") if locator else "1")
            evidence.append({"id": index, "text": text, "status": "待核对" if metadata.get("evidence_status", "来源待核对") != "已核对全文" else "已核对", "source": source})
        return {"summary": self._summary(path, metadata, body), "metadata": metadata, "body": body, "sections": sections, "my_thoughts": extract_my_thoughts(body).strip(), "evidence": evidence}

    def _load_evidence_snapshot(self, relative_path: str) -> Dict[str, Any]:
        path = (self.base_dir / relative_path).resolve()
        if not relative_path or self.base_dir not in path.parents or not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _source_excerpt(self, metadata: Mapping[str, Any], locator_number: str) -> Dict[str, Any]:
        item_key = str(metadata.get("zotero_item_key") or "").strip()
        if not item_key:
            return {"status": "unavailable", "message": "卡片尚未绑定 Zotero 条目。"}
        if _published():
            return {"status": "unavailable", "message": "发布环境不读取本地 PDF 全文；请以条目元数据与卡片内容为准，需要核对原文时使用本地工作台。"}
        try:
            zotero = ZoteroLocalClient()
            attachments = zotero.child_attachments(item_key)
            attachments.sort(key=lambda item: 0 if str(item.get("data", {}).get("contentType", "")).lower() == "application/pdf" or str(item.get("data", {}).get("filename", "")).lower().endswith(".pdf") else 1)
            for attachment in attachments:
                attachment_key = str(attachment.get("key") or attachment.get("data", {}).get("key") or "")
                if not attachment_key:
                    continue
                try:
                    payload = zotero.fulltext(attachment_key)
                except ResearchWorkspaceError:
                    continue
                content = str(payload.get("content") or "").strip()
                if not content:
                    continue
                data = attachment.get("data", {})
                excerpt = content[:2400].strip()
                if len(content) > len(excerpt):
                    excerpt += "…"
                return {"status": "retrieved", "message": "已从 Zotero 全文索引获取；¶定位仍需对照 PDF 核对。", "text": excerpt, "attachment_key": attachment_key, "attachment_name": data.get("filename") or data.get("title") or attachment_key}
        except (ResearchWorkspaceError, requests.RequestException, OSError) as error:
            return {"status": "unavailable", "message": f"暂时无法读取 Zotero 全文：{error}"}
        return {"status": "unavailable", "message": "Zotero 中没有可读取的全文索引附件。"}

    def save_personal(self, citekey: str, payload: Mapping[str, Any], collection: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            path, metadata, body = self._find(citekey, collection)
            current = path.read_text(encoding="utf-8")
            expected = payload.get("base_hash")
            if expected and expected != _sha(current):
                raise ValueError("这张卡片已在其他位置发生变化，请重新加载后再保存。")
            if "reading_status" in payload:
                if payload["reading_status"] not in READING_STATUSES:
                    raise ValueError("无效的阅读状态。")
                metadata["reading_status"] = payload["reading_status"]
            if "evidence_status" in payload:
                metadata["evidence_status"] = str(payload["evidence_status"])
            questions = payload.get("questions")
            if questions is not None:
                if not isinstance(questions, list) or any(not isinstance(item, dict) for item in questions):
                    raise ValueError("疑问必须是对象列表。")
                metadata["questions"] = questions
            generated = current
            if "my_thoughts" in payload:
                replacement = str(payload.get("my_thoughts") or "")
                marker = "## My Thoughts"
                index = body.find(marker)
                if index == -1:
                    body = body.rstrip() + "\n\n" + marker + "\n" + replacement.strip() + "\n"
                else:
                    body = body[: index + len(marker)] + "\n\n" + replacement.strip() + "\n"
                generated = render_front_matter(metadata, body)
            else:
                metadata["updated_at"] = _utc_now()
                generated = render_front_matter(metadata, body)
            self.history_dir.mkdir(parents=True, exist_ok=True)
            backup = self.history_dir / f"{path.stem}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.md"
            backup.write_text(current, encoding="utf-8")
            metadata["updated_at"] = _utc_now()
            generated = render_front_matter(metadata, body)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(generated)
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return self.detail(citekey, collection)

    def update_sync_metadata(self, citekey: str, collection: str, note_key: Optional[str]) -> None:
        with self._lock:
            path, metadata, body = self._find(citekey, collection)
            current = path.read_text(encoding="utf-8")
            metadata["sync_status"] = "已同步"
            if note_key:
                metadata["zotero_note_key"] = note_key
            metadata["updated_at"] = _utc_now()
            self.history_dir.mkdir(parents=True, exist_ok=True)
            backup = self.history_dir / f"{path.stem}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.md"
            backup.write_text(current, encoding="utf-8")
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(render_front_matter(metadata, body))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def compare(self, citekeys: List[str], collection: Optional[str] = None) -> Dict[str, Any]:
        if not 2 <= len(citekeys) <= 4:
            raise ValueError("比较需要选择 2 到 4 篇论文。")
        cards = [self.detail(key, collection) for key in citekeys]
        dimensions = [
            ("研究问题", "Research Questions / Hypotheses"),
            ("理论与概念", "Theory and Key Constructs"),
            ("操作化", "Operationalization"),
            ("数据与样本", "Research Context, Data, and Sample"),
            ("方法", "Method and Analysis"),
            ("发现", "Main Findings"),
            ("局限", "Contributions and Limitations"),
        ]
        rows = [{"label": label, "values": [card["sections"].get(section, "未报告") for card in cards]} for label, section in dimensions]
        return {"cards": [card["summary"] for card in cards], "rows": rows}

    def map_data(self, collection: str) -> Dict[str, Any]:
        cards = [card for card in self.list_cards(collection) if card.get("has_card")]
        theories: Dict[str, Dict[str, Any]] = {}
        methods: Dict[str, Dict[str, Any]] = {}
        links: List[Dict[str, str]] = []
        for card in cards:
            detail = self.detail(card["citekey"], collection)
            for theory in detail["metadata"].get("theories", []):
                if isinstance(theory, dict) and theory.get("name"):
                    name = str(theory["name"]); theories.setdefault(name, {"name": name, "papers": []})["papers"].append(card["citekey"])
            for method in detail["metadata"].get("methods", []):
                if isinstance(method, dict) and method.get("name"):
                    name = str(method["name"]); methods.setdefault(name, {"name": name, "papers": []})["papers"].append(card["citekey"])
            for theory in card["theories"]:
                for method in card["methods"]:
                    links.append({"theory": theory, "method": method, "citekey": card["citekey"]})
        for entry in list(theories.values()) + list(methods.values()):
            entry["papers"] = sorted(set(entry["papers"]))
            entry["count"] = len(entry["papers"])
        return {"collection": collection, "card_count": len(cards), "theories": sorted(theories.values(), key=lambda item: item["name"].casefold()), "methods": sorted(methods.values(), key=lambda item: item["name"].casefold()), "links": links, "incomplete": [card["citekey"] for card in cards if not card["theories"] or not card["methods"]]}


class DailyStore:
    """Read the daily feed and persist feedback (read/starred/useful) back to it."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.feed_path = base_dir / "data" / "daily_feed.jsonl"
        self.feedback_path = base_dir / "data" / "feedback.json"
        self._lock = threading.RLock()

    def _records(self) -> List[Dict[str, Any]]:
        if not self.feed_path.exists():
            return []
        records = []
        for line in self.feed_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def dates(self) -> List[str]:
        return sorted({str(record.get("date")) for record in self._records() if record.get("date")}, reverse=True)

    def day(self, day: Optional[str] = None) -> Dict[str, Any]:
        records = self._records()
        dates = sorted({str(record.get("date")) for record in records if record.get("date")}, reverse=True)
        target = day or (dates[0] if dates else None)
        return {"date": target, "dates": dates, "papers": [record for record in records if record.get("date") == target]}

    def history(self, slot: str = "", venue: str = "", read_state: str = "") -> List[Dict[str, Any]]:
        records = self._records()
        if slot:
            records = [record for record in records if record.get("slot") == slot]
        if venue:
            venue_query = venue.casefold()
            records = [record for record in records if venue_query in str(record.get("venue", "")).casefold()]
        if read_state == "read":
            records = [record for record in records if (record.get("feedback") or {}).get("read")]
        elif read_state == "unread":
            records = [record for record in records if not (record.get("feedback") or {}).get("read")]
        return sorted(records, key=lambda record: (str(record.get("date")), record.get("slot", "")), reverse=True)

    def apply_feedback(self, day: str, dedupe_key: str, field: str, value: Any) -> Dict[str, Any]:
        if field not in {"read", "starred", "useful"}:
            raise ValueError("反馈字段必须是 read、starred 或 useful。")
        with self._lock:
            records = self._records()
            target = next((record for record in records if record.get("date") == day and record.get("dedupe_key") == dedupe_key), None)
            if not target:
                raise KeyError(dedupe_key)
            feedback = target.setdefault("feedback", {"read": False, "starred": False, "useful": None})
            previous_useful = feedback.get("useful")
            if field == "useful":
                feedback["useful"] = value if value in {True, False} else None
            else:
                feedback[field] = bool(value)
            with self.feed_path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if field == "useful":
                self._update_feedback_summary(str(target.get("venue", "")), previous_useful, feedback["useful"])
            return target

    def _update_feedback_summary(self, venue: str, previous: Any, current: Any) -> None:
        if not venue or previous == current:
            return
        try:
            payload = json.loads(self.feedback_path.read_text(encoding="utf-8")) if self.feedback_path.exists() else {}
        except (OSError, ValueError):
            payload = {}
        venues = payload.setdefault("venues", {})
        counts = venues.setdefault(venue, {"useful": 0, "useless": 0})
        if previous is True:
            counts["useful"] = max(0, counts["useful"] - 1)
        elif previous is False:
            counts["useless"] = max(0, counts["useless"] - 1)
        if current is True:
            counts["useful"] += 1
        elif current is False:
            counts["useless"] += 1
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        self.feedback_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AIService:
    def __init__(self, base_dir: Path):
        env_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.managed = bool(env_key)
        self.published = _published()
        if env_key:
            self.base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip().rstrip("/") or "https://api.deepseek.com"
            self.model = os.getenv("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
            self.api_key = env_key
        else:
            self.base_url = "https://api.openai.com/v1"
            self.model = ""
            self.api_key = ""
        self.timeout = 90
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.task_dir = base_dir / "research" / "tasks"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        for path in self.task_dir.glob("*.json"):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
                if task.get("status") in {"排队", "读取材料", "分析中"}:
                    task["status"] = "已中断"
                    path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
                self.tasks[str(task["id"])] = task
            except (OSError, ValueError, KeyError):
                continue

    def _save_task(self, task_id: str) -> None:
        (self.task_dir / f"{task_id}.json").write_text(json.dumps(self.tasks[task_id], ensure_ascii=False, indent=2), encoding="utf-8")

    def public_settings(self) -> Dict[str, Any]:
        return {"base_url": self.base_url, "model": self.model, "configured": bool(self.api_key and self.model), "managed": self.managed, "published": self.published}

    @staticmethod
    def _normalize_base_url(value: Any) -> Tuple[str, Optional[str]]:
        """Return a Chat Completions base URL and an optional user-facing notice."""
        base_url = str(value or "").strip().rstrip("/")
        if not base_url:
            return "https://api.openai.com/v1", None
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("接口地址必须是以 http:// 或 https:// 开头的完整地址。")
        # platform.deepseek.com is the console, not DeepSeek's OpenAI-compatible API.
        if parsed.netloc.lower() == "platform.deepseek.com":
            return "https://api.deepseek.com", "已将 DeepSeek 控制台地址改为 API 地址 https://api.deepseek.com。"
        return base_url, None

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _request_error(response: requests.Response) -> str:
        """Turn provider errors into a useful, secret-free message for the local UI."""
        detail = ""
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
        except ValueError:
            detail = response.text[:300].strip()
        prefix = f"服务返回 {response.status_code}"
        if response.status_code == 401:
            return f"{prefix}：API Key 无效、已过期或不属于这个服务。"
        if response.status_code == 404:
            return f"{prefix}：接口地址或模型名不正确。DeepSeek 应填写 https://api.deepseek.com。"
        if response.status_code == 429:
            suffix = "请求过于频繁或账户当前受限；请稍后重试，并在 DeepSeek API 平台确认账户额度与速率限制。"
            return f"{prefix}：{detail or suffix}"
        return f"{prefix}{'：' + detail if detail else ''}"

    def configure(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if self.managed:
            return {**self.public_settings(), "notice": "DeepSeek 已由服务端环境变量配置；网页提交的设置不会生效，也不会保存。"}
        if self.published:
            return {**self.public_settings(), "notice": "发布环境不接受网页提交的 API Key；请在 Vercel 环境变量中设置 DEEPSEEK_API_KEY 后重新部署。"}
        base_url, notice = self._normalize_base_url(payload.get("base_url") or self.base_url)
        self.base_url = base_url
        self.model = str(payload.get("model") or self.model)
        if payload.get("api_key"):
            self.api_key = str(payload["api_key"])
        return {**self.public_settings(), "notice": notice}

    def test(self) -> Dict[str, Any]:
        if not self.api_key or not self.model:
            message = "请在 Vercel 环境变量中设置 DEEPSEEK_API_KEY 后重新部署。" if self.published else "请先填写模型名和 API Key。"
            return {"ok": False, "message": message}
        try:
            response = requests.post(self._chat_url(), headers={"Authorization": f"Bearer {self.api_key}"}, json={"model": self.model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 8}, timeout=20)
            if not response.ok:
                return {"ok": False, "message": self._request_error(response)}
            return {"ok": True, "message": "连接成功。", "usage": response.json().get("usage")}
        except requests.RequestException as error:
            return {"ok": False, "message": f"无法连接 AI 服务：{error}"}

    def create(self, task_type: str, context: str, citekeys: List[str]) -> str:
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "type": task_type, "citekeys": citekeys, "status": "排队", "created_at": _utc_now(), "result": None, "error": None, "usage": None}
        with self.lock:
            self.tasks[task_id] = task
            self._save_task(task_id)
        self.executor.submit(self._run, task_id, context)
        return task_id

    def create_reading(self, paper: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "type": "read", "citekeys": [paper["citekey"]], "paper": dict(paper), "snapshot": {"path": snapshot["path"], "attachment_key": snapshot["attachment_key"], "chunk_count": len(snapshot["chunks"])}, "status": "排队", "created_at": _utc_now(), "result": None, "error": None, "usage": None, "call_count": 0}
        with self.lock:
            self.tasks[task_id] = task
            self._save_task(task_id)
        self.executor.submit(self._run, task_id, reading_prompt(paper, snapshot))
        return task_id

    def _run(self, task_id: str, context: str) -> None:
        with self.lock:
            self.tasks[task_id]["status"] = "分析中"
            self.tasks[task_id]["call_count"] = self.tasks[task_id].get("call_count", 0) + 1
            self._save_task(task_id)
        try:
            if not self.api_key or not self.model:
                raise RuntimeError("AI 服务尚未配置。")
            prompt = context if self.tasks[task_id].get("type") == "read" else "你是传播学研究助理。只依据给定文献卡内容回答。区分作者原始发现与综合分析；每个事实保留原有 citekey 和证据定位。\n\n" + context
            response = requests.post(self._chat_url(), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"model": self.model, "messages": [{"role": "system", "content": "Return clear Markdown."}, {"role": "user", "content": prompt}], "temperature": 0.2}, timeout=self.timeout)
            if not response.ok:
                raise RuntimeError(self._request_error(response))
            payload = response.json()
            result = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
            with self.lock:
                self.tasks[task_id].update({"status": "待采用", "result": result, "usage": payload.get("usage")})
                self._save_task(task_id)
        except Exception as error:  # task boundary: report failure to UI
            with self.lock:
                self.tasks[task_id].update({"status": "失败", "error": str(error)})
                self._save_task(task_id)

    def get(self, task_id: str) -> Dict[str, Any]:
        with self.lock:
            if task_id not in self.tasks:
                raise KeyError(task_id)
            return dict(self.tasks[task_id])


def create_app(base_dir: Path) -> Flask:
    base_dir = base_dir.resolve()
    app = Flask(__name__, static_folder=str(base_dir / "web"), static_url_path="/assets")
    store, ai, daily = CardStore(base_dir), AIService(base_dir), DailyStore(base_dir)
    session_token = secrets.token_urlsafe(24)

    @app.before_request
    def guard_api() -> Optional[Any]:
        if request.path == "/api/session":
            return None
        if request.path.startswith("/api/") and request.headers.get("X-Research-Session") != session_token:
            return jsonify({"error": "本地会话无效，请刷新网页。"}), 403
        return None

    @app.get("/")
    def index() -> Any:
        return send_from_directory(str(base_dir / "web"), "index.html")

    @app.get("/api/session")
    def session() -> Any:
        return jsonify({"token": session_token})

    @app.get("/api/collections")
    def collections() -> Any:
        return jsonify({"collections": store.collections()})

    @app.get("/api/daily")
    def daily_feed() -> Any:
        return jsonify(daily.day(request.args.get("date")))

    @app.get("/api/daily/history")
    def daily_history() -> Any:
        return jsonify({"papers": daily.history(request.args.get("slot", ""), request.args.get("venue", ""), request.args.get("read_state", ""))})

    @app.post("/api/daily/feedback")
    def daily_feedback() -> Any:
        payload = request.get_json(force=True)
        try:
            record = daily.apply_feedback(str(payload.get("date", "")), str(payload.get("dedupe_key", "")), str(payload.get("field", "")), payload.get("value"))
            return jsonify(record)
        except KeyError:
            return jsonify({"error": "找不到这条每日推荐。"}), 404
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/cards")
    def cards() -> Any:
        return jsonify({"cards": store.list_cards(request.args.get("collection"), request.args.get("q", ""), request.args.get("status"), request.args.get("theory"), request.args.get("method"))})

    @app.get("/api/cards/<citekey>")
    def card(citekey: str) -> Any:
        try:
            return jsonify(store.detail(_safe_id(citekey), request.args.get("collection")))
        except KeyError:
            return jsonify({"error": "找不到这张文献卡。"}), 404

    @app.get("/api/evidence/<citekey>/<int:evidence_id>")
    def evidence(citekey: str, evidence_id: int) -> Any:
        try:
            detail = store.detail(_safe_id(citekey), request.args.get("collection"))
            matches = [item for item in detail["evidence"] if item["id"] == evidence_id]
            if not matches:
                return jsonify({"error": "找不到这条证据。"}), 404
            return jsonify(matches[0])
        except KeyError:
            return jsonify({"error": "找不到这张文献卡。"}), 404

    @app.post("/api/cards/bootstrap")
    def bootstrap_card() -> Any:
        payload = request.get_json(force=True)
        citekey = str(payload.get("citekey", "")).strip()
        title = str(payload.get("title", "")).strip()
        collection = str(payload.get("collection", "")).strip()
        item_key = str(payload.get("zotero_item_key", "")).strip()
        if not all([citekey, title, collection, item_key]):
            return jsonify({"error": "创建卡片需要 citekey、标题、集合和 Zotero item key。"}), 400
        output = store.cards_dir / (re.sub(r"[^A-Za-z0-9._-]", "-", citekey).strip("-") or "untitled")
        output = output.with_suffix(".md")
        if output.exists():
            return jsonify({"error": "这篇论文已经有卡片。"}), 409
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_card_template(citekey, title, collection, item_key), encoding="utf-8")
        return jsonify(store.detail(citekey, collection)), 201

    @app.get("/api/papers/<item_key>/material")
    def paper_material(item_key: str) -> Any:
        try:
            return jsonify(store.reading_store.material(_safe_id(item_key)))
        except ResearchWorkspaceError as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/reading-tasks")
    def reading_task() -> Any:
        payload = request.get_json(force=True)
        item_key = str(payload.get("item_key") or "").strip()
        attachment_key = str(payload.get("attachment_key") or "").strip()
        collection = str(payload.get("collection") or "").strip()
        if not all([item_key, attachment_key, collection]):
            return jsonify({"error": "生成精读需要论文、集合和已索引全文附件。"}), 400
        try:
            paper = store.reading_store.material(item_key)
            paper["collection"] = collection
            snapshot = store.reading_store.snapshot(paper, attachment_key, int(payload.get("chunk_size") or 6000))
            return jsonify({"task_id": ai.create_reading(paper, snapshot), "paper": paper, "snapshot": {"path": snapshot["path"], "chunk_count": len(snapshot["chunks"]), "content_sha256": snapshot["content_sha256"]}}), 202
        except ResearchWorkspaceError as error:
            return jsonify({"error": str(error)}), 400

    @app.patch("/api/cards/<citekey>/personal")
    def personal(citekey: str) -> Any:
        try:
            return jsonify(store.save_personal(_safe_id(citekey), request.get_json(force=True), request.args.get("collection")))
        except (KeyError, ValueError) as error:
            return jsonify({"error": str(error)}), 409 if "其他位置" in str(error) else 400

    @app.post("/api/compare")
    def compare() -> Any:
        try:
            payload = request.get_json(force=True)
            return jsonify(store.compare(list(payload.get("citekeys", [])), payload.get("collection")))
        except (KeyError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/map")
    def map_data() -> Any:
        collection = request.args.get("collection")
        if not collection:
            return jsonify({"error": "需要指定集合。"}), 400
        return jsonify(store.map_data(collection))

    @app.get("/api/settings")
    def settings() -> Any:
        web_configured = bool(os.getenv("ZOTERO_USER_ID", "").strip() and os.getenv("ZOTERO_API_KEY", "").strip())
        if _published():
            zotero_status: Dict[str, Any] = {"mode": "web"}
            try:
                client = zotero_web_client_from_env()
                zotero_status.update({"connected": True, "collection_count": len(client.collections()), "writable": client.can_write()})
            except ResearchWorkspaceError as error:
                zotero_status.update({"connected": False, "writable": False, "error": str(error)})
        else:
            try:
                zotero = ZoteroLocalClient(); zotero.collections()
                zotero_status = {"mode": "local", "api_url": DEFAULT_ZOTERO_URL, "bbt_url": DEFAULT_BBT_URL, "version": zotero.last_version}
            except ResearchWorkspaceError as error:
                zotero_status = {"mode": "local", "api_url": DEFAULT_ZOTERO_URL, "bbt_url": DEFAULT_BBT_URL, "error": str(error)}
            zotero_status["writable"] = web_configured
        zotero_status["web_api"] = {"configured": web_configured, "hint": None if web_configured else "设置 ZOTERO_USER_ID 与 ZOTERO_API_KEY 后即可同步精读卡到 Zotero（zotero.org/settings/keys，需勾选写权限）。"}
        return jsonify({"ai": ai.public_settings(), "zotero": zotero_status})

    @app.patch("/api/settings/ai")
    def settings_ai() -> Any:
        return jsonify(ai.configure(request.get_json(force=True)))

    @app.post("/api/settings/ai/test")
    def test_ai() -> Any:
        return jsonify(ai.test())

    @app.post("/api/ai")
    def ai_task() -> Any:
        payload = request.get_json(force=True)
        citekeys = list(payload.get("citekeys", []))
        if not citekeys:
            return jsonify({"error": "至少选择一篇论文。"}), 400
        try:
            details = [store.detail(key, payload.get("collection")) for key in citekeys]
        except KeyError:
            return jsonify({"error": "所选论文中有文献卡不存在。"}), 400
        chunks = []
        for detail in details:
            source_body = detail["body"]
            if not payload.get("include_personal"):
                source_body = source_body.split("## My Thoughts", 1)[0]
            chunks.append(f"## {detail['summary']['citekey']} — {detail['summary']['title']}\n\n{source_body}")
        task_type = str(payload.get("type", "read"))
        instruction = "请生成结构化精读修订稿。" if task_type == "read" else "请比较这些论文的研究问题、理论、操作化、方法、发现和局限，并区分不可直接比较之处。"
        return jsonify({"task_id": ai.create(task_type, instruction + "\n\n" + "\n\n".join(chunks), citekeys)})

    @app.get("/api/tasks/<task_id>")
    def task(task_id: str) -> Any:
        try:
            return jsonify(ai.get(task_id))
        except KeyError:
            return jsonify({"error": "找不到任务。"}), 404

    @app.post("/api/tasks/<task_id>/adopt")
    def adopt_task(task_id: str) -> Any:
        try:
            task_data = ai.get(task_id)
        except KeyError:
            return jsonify({"error": "找不到任务。"}), 404
        if task_data.get("type") != "read" or task_data.get("status") != "待采用" or not task_data.get("result"):
            return jsonify({"error": "只有完成的精读草稿可以采用。"}), 409
        paper = task_data.get("paper") or {}
        snapshot_path = str((task_data.get("snapshot") or {}).get("path") or "")
        snapshot_file = (base_dir / snapshot_path).resolve()
        if base_dir not in snapshot_file.parents or not snapshot_file.is_file():
            return jsonify({"error": "精读证据快照已不存在，无法采用。"}), 409
        snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
        output = store.cards_dir / (re.sub(r"[^A-Za-z0-9._-]", "-", str(paper["citekey"])).strip("-") or "untitled")
        output = output.with_suffix(".md")
        if output.exists():
            return jsonify({"error": "正式卡片已存在；请先查看并决定是否保留现有卡片。"}), 409
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(card_document(paper, snapshot, str(task_data["result"])), encoding="utf-8")
        task_data["status"] = "已采用"
        ai._save_task(task_id)
        return jsonify(store.detail(str(paper["citekey"]), str(paper["collection"]))), 201

    @app.post("/api/cards/<citekey>/sync")
    def sync_card(citekey: str) -> Any:
        try:
            _, metadata, _ = store._find(_safe_id(citekey), request.args.get("collection"))
            collection = str(metadata["collection"])
            report = sync_zotero_notes(store.cards_dir, collection, base_dir / "research" / "zotero-sync" / "notes.json", zotero_web_client_from_env(), dry_run=False)
            result = next((item for item in report if Path(str(item.get("card_path", ""))).name == f"{citekey}.md"), None)
            if not result or result.get("status") not in {"created", "updated", "unchanged"}:
                raise ResearchWorkspaceError(str((result or {}).get("issues", ["同步未完成"])[0]))
            store.update_sync_metadata(_safe_id(citekey), collection, result.get("note_key"))
            return jsonify(result)
        except (KeyError, ResearchWorkspaceError) as error:
            return jsonify({"error": str(error)}), 409

    @app.post("/api/tasks/<task_id>/draft")
    def task_draft(task_id: str) -> Any:
        try:
            task_data = ai.get(task_id)
        except KeyError:
            return jsonify({"error": "找不到任务。"}), 404
        if task_data.get("status") != "待采用" or not task_data.get("result"):
            return jsonify({"error": "任务尚未产生可保存的草稿。"}), 409
        draft_dir = base_dir / "research" / "drafts"
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_path = draft_dir / f"{task_id}.md"
        draft_path.write_text("# Codex AI 草稿\n\n" + str(task_data["result"]) + "\n", encoding="utf-8")
        return jsonify({"path": str(draft_path.relative_to(base_dir))})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地 Zotero 科研网页工作台")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    create_app(Path(args.base_dir)).run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
