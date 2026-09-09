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
    split_front_matter,
    sync_zotero_notes,
    validate_card,
)
from .config import load_settings
from .daily import run_daily
from .scoring import venue_meta
from .database import DatabaseUnavailable, ResearchDatabase
from .research_reading import ReadingMaterialStore, card_document, evidence_ids, reading_prompt


READING_STATUSES = ("待读", "初读", "精读中", "精读完成")
HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)
EVIDENCE_RE = re.compile(r"\[([^\]]+?\s*(?:¶|段落|paragraph)\s*\d+)\]", re.IGNORECASE)
EVIDENCE_LOCATOR_RE = re.compile(r"(?P<citekey>[^\s\[\]]+)\s*(?:¶|段落|paragraph)\s*(?P<number>\d+)", re.IGNORECASE)


def _published() -> bool:
    return bool(os.environ.get("VERCEL"))


def _use_zotero_web_api() -> bool:
    """Use Zotero Cloud on Vercel and in v0 Preview; regular local runs keep Desktop API support."""
    return _published() or os.environ.get("ZOTERO_USE_WEB_API") == "1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SECTION_HEADING_RE = re.compile(r"^#{1,2}\s+(.+?)\s*$", re.MULTILINE)
SUBHEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(body: str) -> Dict[str, str]:
    """Split on H1/H2 only; H3 subheadings stay inside their parent section as highlighted lines."""
    matches = list(SECTION_HEADING_RE.finditer(body))
    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = SUBHEADING_RE.sub(lambda sub: f"◆ {sub.group(1)}", body[start:end]).strip()
        sections[title] = content
    return sections


def _safe_id(value: str) -> str:
    return value.strip()


def _duration_seconds(start: Any, end: Any) -> Optional[float]:
    try:
        return round((datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def _parse_ai_json(content: str) -> Dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("AI 未返回可解析的 JSON。")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回的 JSON 不是对象。")
    return parsed


COMPARE_DIMENSIONS: List[Any] = [
    ("阅读概览", ["阅读概览"]),
    ("研究问题", ["研究问题", "Research Questions / Hypotheses"]),
    ("理论与概念", ["理论与概念", "Theory and Key Constructs"]),
    ("操作化", ["操作化", "Operationalization"]),
    ("数据与样本", ["数据与样本", "Research Context, Data, and Sample"]),
    ("方法", ["方法", "Method and Analysis"]),
    ("发现", ["主要发现", "Main Findings"]),
    ("局限", ["贡献与局限", "Contributions and Limitations"]),
]


def _dimension_rows(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def pick(card: Dict[str, Any], names: List[str]) -> str:
        for name in names:
            value = str(card["sections"].get(name) or "").strip()
            if value and value != "本次材料未覆盖。":
                return value
        return "未报告"

    rows = [{"label": label, "values": [pick(card, names) for card in cards]} for label, names in COMPARE_DIMENSIONS]
    return [row for row in rows if any(value != "未报告" for value in row["values"])]


def _group_synthesis_prompt(group: Mapping[str, Any]) -> str:
    papers = []
    for index, card in enumerate(group["cards"]):
        parts = [f"论文{index + 1}：{card.get('title', '')}（{card.get('citekey', '')}）"]
        for row in group["rows"]:
            value = str(row["values"][index] or "")
            if value and value != "未报告":
                parts.append(f"  {row['label']}：{value[:400]}")
        papers.append("\n".join(parts))
    corpus = "\n\n".join(papers)[:9000]
    return f"""你是传播学领域的综述作者。以下是「{group['label']}」类别下的 {len(group['cards'])} 篇精读卡内容。请输出一个 JSON 对象（不要解释文字、不要代码块），字段：
{{"overview": "这组论文的整体图景与共同关切，两三句", "convergence": "各篇趋同的发现或共识，一两句", "divergence": "分歧、张力或方法路径差异，一两句", "gaps": "这组文献留下的研究缺口与可行的下一步选题，一两句", "reading_order": "建议的阅读顺序（用论文序号或短标题）及一句理由"}}

{corpus}"""


def _theory_method_prompt(body: str) -> str:
    return f"""从下面的传播学论文精读卡内容中，抽取论文实际使用的理论和研究方法。输出一个 JSON 对象（不要解释、不要代码块），字段：
{{"theories": ["理论名称，用领域通用的简短中文名，如：框架理论、议程设置、第三人效果；最多 4 个；没有明确理论则为空数组"], "methods": ["方法名称，用通用的简短中文名，如：问卷调查、实验、内容分析、深度访谈、计算文本分析；最多 4 个"]}}

精读卡内容：
{body[:6000]}"""


def _daily_review_prompt(paper: Mapping[str, Any]) -> str:
    abstract = str(paper.get("abstract") or "").strip() or "（无公开摘要，请基于标题与期刊推断，并明确说明是推断）"
    return f"""你是资深传播学期刊审稿人兼研究导师。请针对下面这篇每日推荐论文，输出一个 JSON 对象（不要解释文字、不要代码块），字段：
{{"abstract_zh": "摘要的完整中文翻译，保持学术准确；若无摘要则为空字符串", "verdict": "推荐评语：两三句话，说明这篇论文最值得读的理由和适合谁读", "highlights": "研究亮点：选题新意、数据独特性或发现的贡献，一两句", "method_review": "方法评价：设计是否严谨、证据强度如何，一两句", "relevance": "对传播学研究者的可借鉴之处：理论对话、测量、写作或选题上能学什么，一两句", "caution": "阅读提醒：局限、边界条件或需要谨慎解读的地方，一两句"}}

论文标题：{paper.get('title', '')}
作者：{'; '.join(paper.get('authors', [])[:8]) or '未知'}
期刊：{paper.get('venue', '') or '未知'}
发表时间：{paper.get('publication_date', '') or '未知'}
推荐槽位：{paper.get('slot_label', '')}
摘要原文：{abstract}"""


def _enrichment_prompt(paper: Mapping[str, Any]) -> str:
    tags = "、".join(paper.get("tags", [])) or "无"
    abstract = str(paper.get("abstract") or "").strip() or "（无摘要）"
    return f"""你是传播学研究助理。根据下面论文的元数据，输出一个 JSON 对象（不要任何解释文字、不要代码块），字段：
{{"abstract_zh": "摘要的完整中文翻译；若无摘要则为空字符串", "keywords": ["3-8 个中文关键词"], "tags": ["3-5 个研究标签，例如：定量、实验、内容分析、框架理论、健康传播、政治传播、计算方法"], "quick_take": "一到两句话说明这篇论文做了什么、核心发现或价值", "method": "研究方法一句话概括；无法判断写空字符串", "theory": "核心理论；无法判断写空字符串"}}

论文标题：{paper.get('title', '')}
作者：{'; '.join(paper.get('authors', [])) or '未知'}
期刊：{paper.get('journal', '') or '未知'}
发表时间：{paper.get('date', '') or '未知'}
Zotero 标签：{tags}
摘要原文：{abstract}"""


class CardStore:
    def __init__(self, base_dir: Path, database: Optional[ResearchDatabase] = None):
        self.base_dir = base_dir.resolve()
        self.cards_dir = self.base_dir / "research" / "cards"
        self.collections_dir = self.base_dir / "research" / "collections"
        self.history_dir = self.base_dir / "research" / "history" / "cards"
        self.database = database or ResearchDatabase("")
        self.reading_store = ReadingMaterialStore(self.base_dir, cloud=_use_zotero_web_api())
        self._lock = threading.RLock()

    def collections(self) -> List[str]:
        values = {str(meta.get("collection")) for path in self.cards_dir.glob("*.md") for meta in [self._load(path)[0]] if meta.get("collection")}
        if self.database.available:
            try:
                values.update(str(card["metadata"].get("collection")) for card in self.database.reading_cards() if card["metadata"].get("collection"))
            except (DatabaseUnavailable, TimeoutError, OSError):
                pass
        try:
            client = zotero_web_client_from_env() if _use_zotero_web_api() else ZoteroLocalClient()
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

    def _summary_data(self, metadata: Dict[str, Any], body: str, path: Optional[Path] = None) -> Dict[str, Any]:
        fallback = path.stem if path else "untitled"
        citekey = str(metadata.get("citekey", fallback))
        questions = metadata.get("questions", [])
        if not isinstance(questions, list):
            questions = []
        return {
            "id": citekey,
            "citekey": citekey,
            "title": str(metadata.get("title", fallback)),
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
            "path": str(path.relative_to(self.base_dir)) if path else None,
            "hash": _sha(path.read_text(encoding="utf-8")) if path else _sha(body),
            "has_card": True,
        }

    def _summary(self, path: Path, metadata: Dict[str, Any], body: str) -> Dict[str, Any]:
        return self._summary_data(metadata, body, path)

    def list_cards(self, collection: Optional[str] = None, query: str = "", status: Optional[str] = None, theory: Optional[str] = None, method: Optional[str] = None) -> List[Dict[str, Any]]:
        query = query.strip().casefold()
        result: List[Dict[str, Any]] = []
        if self.database.available:
            try:
                for card in self.database.reading_cards(collection):
                    metadata, body = dict(card["metadata"]), str(card["content"])
                    summary = self._summary_data(metadata, body)
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
            except (DatabaseUnavailable, TimeoutError, OSError):
                pass
        persistent_keys = {item.get("zotero_item_key") for item in result}
        for path in sorted(self.cards_dir.glob("*.md")):
            try:
                metadata, body = self._load(path)
            except (OSError, ValueError):
                continue
            if collection and metadata.get("collection") != collection:
                continue
            if metadata.get("zotero_item_key") in persistent_keys:
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
            card_item_keys = {item["zotero_item_key"] for item in result if item.get("has_card") and item.get("zotero_item_key")}
            meta_map, items = self.zotero_collection_data(collection)
            result.extend(self._uncarded_from_items(items, collection, query, status, theory, method, card_item_keys))
            enriched = self.enrichment_map()
            for summary in result:
                item_key = summary.get("zotero_item_key")
                if item_key and item_key in meta_map:
                    summary["paper_meta"] = meta_map[item_key]
                if item_key and item_key in enriched:
                    summary["enrichment"] = enriched[item_key]
        return result

    def _settings(self) -> Dict[str, Any]:
        if not hasattr(self, "_settings_cache"):
            try:
                self._settings_cache = load_settings(str(self.base_dir / "config" / "settings.yml"))
            except Exception:
                self._settings_cache = {}
        return self._settings_cache

    def zotero_collection_data(self, collection: str) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        """Return per-item bibliographic metadata plus the raw Zotero items for a collection."""
        try:
            zotero = zotero_web_client_from_env() if _use_zotero_web_api() else ZoteroLocalClient()
            zotero_collection = zotero.collection_by_name(collection)
            items = zotero.collection_items(str(zotero_collection.get("key")))
        except Exception:
            return {}, []
        settings = self._settings()
        metas: Dict[str, Dict[str, Any]] = {}
        for item in items:
            data = item.get("data", {})
            item_key = str(item.get("key") or data.get("key") or "")
            if not item_key:
                continue
            authors = []
            for creator in data.get("creators") or []:
                name = " ".join(part for part in [creator.get("firstName"), creator.get("lastName")] if part) or str(creator.get("name") or "")
                if name:
                    authors.append(name)
            journal = str(data.get("publicationTitle") or data.get("proceedingsTitle") or data.get("bookTitle") or "")
            meta: Dict[str, Any] = {
                "item_key": item_key,
                "title": str(data.get("title") or ""),
                "authors": authors,
                "date": str(data.get("date") or ""),
                "journal": journal,
                "abstract": str(data.get("abstractNote") or ""),
                "tags": [str(tag.get("tag")) for tag in data.get("tags") or [] if tag.get("tag")],
                "doi": str(data.get("DOI") or ""),
                "item_type": str(data.get("itemType") or ""),
            }
            if journal and settings:
                try:
                    venue = venue_meta(journal, settings)
                    if venue.get("if_2025") or venue.get("rank"):
                        meta["venue_meta"] = {"if_2025": venue.get("if_2025"), "rank": venue.get("rank"), "group": venue.get("group")}
                except Exception:
                    pass
            metas[item_key] = meta
        return metas, items

    def enrichment_map(self) -> Dict[str, Dict[str, Any]]:
        if self.database.available:
            try:
                return self.database.enrichments()
            except (DatabaseUnavailable, TimeoutError, OSError):
                pass
        try:
            payload = json.loads((self.base_dir / "research" / "enrichments.json").read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_enrichment(self, item_key: str, collection: str, data: Mapping[str, Any]) -> None:
        if self.database.available:
            try:
                self.database.save_enrichment(item_key, collection, data)
                return
            except (DatabaseUnavailable, TimeoutError, OSError):
                pass
        path = self.base_dir / "research" / "enrichments.json"
        with self._lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, ValueError):
                payload = {}
            payload[item_key] = dict(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _uncarded_from_items(self, items: List[Dict[str, Any]], collection: str, query: str, status: Optional[str], theory: Optional[str], method: Optional[str], card_item_keys: set) -> List[Dict[str, Any]]:
        """Show Zotero papers that have no Markdown card yet, without creating one."""
        try:
            keys = [str(item.get("key") or item.get("data", {}).get("key") or "") for item in items]
            citekeys = BetterBibTeXClient().citationkeys(keys) if keys and not _use_zotero_web_api() else {}
        except Exception:
            citekeys = {}
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
        path: Optional[Path]
        try:
            path, metadata, body = self._find(citekey, collection)
        except KeyError:
            card = self.database.reading_card(citekey, collection) if self.database.available else None
            if not card:
                raise
            path, metadata, body = None, dict(card["metadata"]), str(card["content"])
        sections = _split_sections(body)
        evidence = []
        evidence_file = str(metadata.get("evidence_file") or "")
        snapshot = metadata.get("_evidence_snapshot") or self._load_evidence_snapshot(evidence_file)
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
        public_metadata = {key: value for key, value in metadata.items() if not key.startswith("_")}
        return {"summary": self._summary_data(public_metadata, body, path), "metadata": public_metadata, "body": body, "sections": sections, "my_thoughts": extract_my_thoughts(body).strip(), "evidence": evidence}

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
            try:
                path, metadata, body = self._find(citekey, collection)
            except KeyError:
                card = self.database.reading_card(citekey, collection) if self.database.available else None
                if not card:
                    raise
                metadata = dict(card["metadata"])
                metadata["sync_status"] = "已同步"
                if note_key:
                    metadata["zotero_note_key"] = note_key
                metadata["updated_at"] = _utc_now()
                self.database.update_reading_card_metadata(str(card["item_key"]), metadata)
                return
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
        return {"cards": [card["summary"] for card in cards], "rows": _dimension_rows(cards)}

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
        return {
            "collection": collection,
            "card_count": len(cards),
            "theories": sorted(theories.values(), key=lambda item: item["name"].casefold()),
            "methods": sorted(methods.values(), key=lambda item: item["name"].casefold()),
            "links": links,
            "titles": {card["citekey"]: str(card.get("title") or card["citekey"]) for card in cards},
            "incomplete": [card["citekey"] for card in cards if not card["theories"] or not card["methods"]],
        }

    def group(self, collection: str, theory: str = "", method: str = "") -> Dict[str, Any]:
        if not theory and not method:
            raise ValueError("请选择理论或方法类别。")
        data = self.map_data(collection)
        keys = sorted({link["citekey"] for link in data["links"] if (not theory or link["theory"] == theory) and (not method or link["method"] == method)})
        if not keys:
            raise ValueError("这个类别下暂时没有精读卡。")
        cards = [self.detail(key, collection) for key in keys[:8]]
        label = " × ".join(part for part in (theory, method) if part)
        return {"label": label, "theory": theory, "method": method, "citekeys": keys, "cards": [card["summary"] for card in cards], "rows": _dimension_rows(cards)}


class DailyStore:
    """Persist the daily feed in Neon, with file fallback for local CLI use."""

    def __init__(self, base_dir: Path, database: Optional[ResearchDatabase] = None):
        self.base_dir = base_dir
        self.feed_path = base_dir / "data" / "daily_feed.jsonl"
        self.feedback_path = base_dir / "data" / "feedback.json"
        self.database = database or ResearchDatabase()
        self._lock = threading.RLock()
        self._generating: set = set()

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

    def _persistent_records(self) -> List[Dict[str, Any]]:
        if self.database.available:
            try:
                return self.database.history()
            except (DatabaseUnavailable, TimeoutError, OSError):
                pass
        return self._records()

    def dates(self) -> List[str]:
        return sorted({str(record.get("date")) for record in self._persistent_records() if record.get("date")}, reverse=True)

    def day(self, day: Optional[str] = None) -> Dict[str, Any]:
        if self.database.available:
            try:
                return self.database.day(day)
            except (DatabaseUnavailable, TimeoutError, OSError):
                pass
        records = self._records()
        dates = sorted({str(record.get("date")) for record in records if record.get("date")}, reverse=True)
        target = day or datetime.now(timezone.utc).date().isoformat()
        return {"date": target, "dates": dates, "papers": [record for record in records if record.get("date") == target]}

    def generate(self) -> Dict[str, Any]:
        day = datetime.now(timezone.utc).date().isoformat()
        existing = self.day(day)
        if len(existing["papers"]) >= 3:
            return {**existing, "generated": False}
        with self._lock:
            if day in self._generating:
                return {**existing, "generated": False, "generating": True}
            self._generating.add(day)
        try:
            return self._generate_for(day)
        finally:
            with self._lock:
                self._generating.discard(day)

    def _generate_for(self, day: str) -> Dict[str, Any]:
        settings = load_settings(str(self.base_dir / "config" / "settings.yml"))
        excluded = self.database.excluded_paper_ids() if self.database.available else set()
        records = run_daily(settings, self.base_dir, day=day, dry_run=True, excluded_keys=excluded)
        if not records:
            raise ResearchWorkspaceError("外部检索没有找到适合今日三个槽位的论文，请稍后重试。")
        if self.database.available:
            records = self.database.save_recommendations(day, records)
        else:
            with self._lock:
                current = self._records()
                if not any(record.get("date") == day for record in current):
                    self.feed_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.feed_path.open("a", encoding="utf-8") as handle:
                        for record in records:
                            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {**self.day(day), "generated": True}

    def history(self, slot: str = "", venue: str = "", read_state: str = "") -> List[Dict[str, Any]]:
        records = self._persistent_records()
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
        if self.database.available:
            return self.database.apply_feedback(day, dedupe_key, field, value)
        if field not in {"read", "starred", "useful"}:
            raise ValueError("反馈字段必须是 read、starred 或 useful。")
        with self._lock:
            records = self._records()
            target = next((record for record in records if record.get("date") == day and record.get("dedupe_key") == dedupe_key), None)
            if not target:
                raise KeyError(dedupe_key)
            feedback = target.setdefault("feedback", {"read": False, "starred": False, "useful": None})
            previous_useful = feedback.get("useful")
            feedback[field] = value if field == "useful" and value in {True, False} else (None if field == "useful" else bool(value))
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
        counts = payload.setdefault("venues", {}).setdefault(venue, {"useful": 0, "useless": 0})
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
        self.task_dir = (Path(tempfile.gettempdir()) if self.published else base_dir) / "research" / "tasks"
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
            return f"{prefix}：API Key ��效、已过期或不属于这个服务。"
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
        label = f"AI 比较 · {len(citekeys)} 篇" if task_type == "compare" else f"AI 分析 · {len(citekeys)} 篇"
        task = {"id": task_id, "type": task_type, "label": label, "citekeys": citekeys, "status": "排队", "created_at": _utc_now(), "result": None, "error": None, "usage": None}
        with self.lock:
            self.tasks[task_id] = task
            self._save_task(task_id)
        self.executor.submit(self._run, task_id, context)
        return task_id

    def create_reading(self, paper: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "type": "read", "label": f"精读生成 · {str(paper.get('title', ''))[:48]}", "citekeys": [paper["citekey"]], "paper": dict(paper), "snapshot": {"path": snapshot["path"], "temp_path": snapshot.get("temp_path"), "attachment_key": snapshot["attachment_key"], "content_sha256": snapshot["content_sha256"], "chunk_count": len(snapshot["chunks"])}, "status": "排队", "created_at": _utc_now(), "result": None, "error": None, "usage": None, "call_count": 0}
        with self.lock:
            self.tasks[task_id] = task
            self._save_task(task_id)
        self.executor.submit(self._run, task_id, reading_prompt(paper, snapshot))
        return task_id

    def _chat(self, prompt: str, system: str = "Return clear Markdown.") -> Tuple[str, Any]:
        if not self.api_key or not self.model:
            raise RuntimeError("AI 服务尚未配置。")
        response = requests.post(self._chat_url(), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json={"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "temperature": 0.2}, timeout=self.timeout)
        if not response.ok:
            raise RuntimeError(self._request_error(response))
        payload = response.json()
        return payload.get("choices", [{}])[0].get("message", {}).get("content", ""), payload.get("usage")

    def _finish(self, task_id: str, updates: Dict[str, Any]) -> None:
        with self.lock:
            task = self.tasks[task_id]
            task.update(updates)
            task["finished_at"] = _utc_now()
            task["duration_seconds"] = _duration_seconds(task.get("started_at") or task.get("created_at"), task["finished_at"])
            self._save_task(task_id)

    def _run(self, task_id: str, context: str) -> None:
        with self.lock:
            self.tasks[task_id]["status"] = "分析中"
            self.tasks[task_id]["started_at"] = _utc_now()
            self.tasks[task_id]["call_count"] = self.tasks[task_id].get("call_count", 0) + 1
            self._save_task(task_id)
        try:
            prompt = context if self.tasks[task_id].get("type") == "read" else "你是传播学研究助理。只依据给定文献卡内容回答。区分作者原始发现与综合分析；每个事实保留原有 citekey 和证据定位。\n\n" + context
            result, usage = self._chat(prompt)
            self._finish(task_id, {"status": "待采用" if self.tasks[task_id].get("type") == "read" else "完成", "result": result, "usage": usage})
        except Exception as error:  # task boundary: report failure to UI
            self._finish(task_id, {"status": "失败", "error": str(error)})

    def create_enrichment(self, papers: List[Dict[str, Any]], save: Any) -> str:
        task_id = uuid.uuid4().hex
        task = {"id": task_id, "type": "enrich", "label": f"AI 标签与翻译 · {len(papers)} 篇", "citekeys": [], "status": "排队", "created_at": _utc_now(), "result": None, "error": None, "usage": None, "progress": {"done": 0, "total": len(papers), "current": ""}, "items": []}
        with self.lock:
            self.tasks[task_id] = task
            self._save_task(task_id)
        self.executor.submit(self._run_enrichment, task_id, papers, save)
        return task_id

    def _run_enrichment(self, task_id: str, papers: List[Dict[str, Any]], save: Any) -> None:
        with self.lock:
            self.tasks[task_id]["status"] = "分析中"
            self.tasks[task_id]["started_at"] = _utc_now()
            self._save_task(task_id)
        failures = 0
        for paper in papers:
            title = str(paper.get("title") or "")
            with self.lock:
                self.tasks[task_id]["progress"]["current"] = title
                self._save_task(task_id)
            try:
                content, _usage = self._chat(_enrichment_prompt(paper), system="Return only valid JSON, no markdown.")
                data = _parse_ai_json(content)
                data["generated_at"] = _utc_now()
                save(paper, data)
                entry = {"title": title, "ok": True}
            except Exception as error:  # per-paper boundary: keep processing the batch
                failures += 1
                entry = {"title": title, "ok": False, "error": str(error)}
            with self.lock:
                self.tasks[task_id]["progress"]["done"] += 1
                self.tasks[task_id]["items"].append(entry)
                self._save_task(task_id)
        status = "失败" if failures == len(papers) else ("部分完成" if failures else "完成")
        self._finish(task_id, {"status": status, "error": f"{failures} 篇处理失败" if failures else None})

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self.lock:
            tasks = [dict(task) for task in self.tasks.values()]
        tasks.sort(key=lambda task: str(task.get("created_at") or ""), reverse=True)
        return tasks[:50]

    def get(self, task_id: str) -> Dict[str, Any]:
        with self.lock:
            if task_id not in self.tasks:
                raise KeyError(task_id)
            return dict(self.tasks[task_id])


def create_app(base_dir: Path) -> Flask:
    base_dir = base_dir.resolve()
    app = Flask(__name__, static_folder=str(base_dir / "web"), static_url_path="/assets")
    database = ResearchDatabase()
    store, ai, daily = CardStore(base_dir, database), AIService(base_dir), DailyStore(base_dir, database)
    session_token = secrets.token_urlsafe(24)

    @app.before_request
    def guard_api() -> Optional[Any]:
        if request.path == "/api/session" or _published():
            return None
        if request.path.startswith("/api/") and request.headers.get("X-Research-Session") != session_token:
            return jsonify({"error": "本地会话无效，请刷新网页。"}), 403
        return None

    @app.after_request
    def disable_unversioned_asset_cache(response: Any) -> Any:
        if request.path == "/" or request.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/")
    def index() -> Any:
        return send_from_directory(str(base_dir / "web"), "index.html")

    @app.get("/api/session")
    def session() -> Any:
        return jsonify({"token": session_token})

    @app.get("/api/collections")
    def collections() -> Any:
        return jsonify({"collections": store.collections()})

    def _attach_daily_reviews(papers: List[Dict[str, Any]]) -> None:
        reviews = store.enrichment_map()
        for paper in papers:
            review = reviews.get(f"daily:{paper.get('dedupe_key')}")
            if review:
                paper["ai_review"] = review

    @app.get("/api/daily")
    def daily_feed() -> Any:
        result = daily.day(request.args.get("date"))
        _attach_daily_reviews(result.get("papers", []))
        return jsonify(result)

    @app.post("/api/daily/analyze")
    def daily_analyze() -> Any:
        payload = request.get_json(force=True)
        day_value = str(payload.get("date") or "").strip()
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if not day_value or not dedupe_key:
            return jsonify({"error": "缺少日期或论文标识。"}), 400
        paper = next((entry for entry in daily.day(day_value).get("papers", []) if entry.get("dedupe_key") == dedupe_key), None)
        if not paper:
            return jsonify({"error": "找不到这条每日推荐。"}), 404
        try:
            content, _usage = ai._chat(_daily_review_prompt(paper), system="Return only valid JSON, no markdown.")
            data = _parse_ai_json(content)
            data["generated_at"] = _utc_now()
            store.save_enrichment(f"daily:{dedupe_key}", "daily", data)
            return jsonify({"ai_review": data})
        except (RuntimeError, requests.RequestException, ValueError) as error:
            return jsonify({"error": str(error)}), 502

    @app.post("/api/daily/generate")
    def generate_daily_feed() -> Any:
        try:
            return jsonify(daily.generate())
        except (ResearchWorkspaceError, DatabaseUnavailable, requests.RequestException, TimeoutError) as error:
            return jsonify({"error": str(error)}), 503

    @app.get("/api/daily/history")
    def daily_history() -> Any:
        papers = daily.history(request.args.get("slot", ""), request.args.get("venue", ""), request.args.get("read_state", ""))
        _attach_daily_reviews(papers)
        return jsonify({"dates": daily.dates(), "papers": papers})

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

    @app.post("/api/reading-tasks/batch")
    def reading_tasks_batch() -> Any:
        payload = request.get_json(force=True)
        item_keys = [str(key) for key in payload.get("item_keys", []) if key][:6]
        collection = str(payload.get("collection") or "").strip()
        if not item_keys or not collection:
            return jsonify({"error": "请先勾选论文并指定集合。"}), 400
        created, errors = [], []
        for item_key in item_keys:
            try:
                paper = store.reading_store.material(item_key)
                paper["collection"] = collection
                attachment = next((entry for entry in paper["attachments"] if entry.get("indexed")), None)
                if not attachment:
                    errors.append({"item_key": item_key, "title": paper.get("title", ""), "error": "没有已索引的 PDF 附件"})
                    continue
                snapshot = store.reading_store.snapshot(paper, str(attachment["key"]))
                created.append({"item_key": item_key, "citekey": paper["citekey"], "title": paper.get("title", ""), "task_id": ai.create_reading(paper, snapshot)})
            except (ResearchWorkspaceError, requests.RequestException, OSError, ValueError) as error:
                errors.append({"item_key": item_key, "error": str(error)})
        return jsonify({"created": created, "errors": errors}), 202

    @app.post("/api/papers/enrich")
    def enrich_papers() -> Any:
        payload = request.get_json(force=True)
        item_keys = [str(key) for key in payload.get("item_keys", []) if key][:12]
        collection = str(payload.get("collection") or "").strip()
        if not item_keys or not collection:
            return jsonify({"error": "请先勾选论文并指定集合。"}), 400
        meta_map, _items = store.zotero_collection_data(collection)
        papers = [meta_map[key] for key in item_keys if key in meta_map]
        if not papers:
            return jsonify({"error": "所选论文在 Zotero 集合中找不到。"}), 404
        task_id = ai.create_enrichment(papers, lambda paper, data: store.save_enrichment(str(paper["item_key"]), collection, data))
        return jsonify({"task_id": task_id, "count": len(papers)}), 202

    @app.get("/api/tasks")
    def tasks_list() -> Any:
        return jsonify({"tasks": ai.list_tasks()})

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

    def _group_cache_key(collection: str, theory: str, method: str) -> str:
        return f"mapgroup:{collection}:{theory}|{method}"

    @app.get("/api/map/group")
    def map_group() -> Any:
        collection = request.args.get("collection") or ""
        theory = (request.args.get("theory") or "").strip()
        method = (request.args.get("method") or "").strip()
        if not collection:
            return jsonify({"error": "需要指定集合。"}), 400
        try:
            result = store.group(collection, theory, method)
        except ValueError as error:
            return jsonify({"error": str(error)}), 404
        cached = store.enrichment_map().get(_group_cache_key(collection, theory, method))
        if cached:
            result["synthesis"] = cached
        return jsonify(result)

    @app.post("/api/map/group/analyze")
    def map_group_analyze() -> Any:
        payload = request.get_json(force=True)
        collection = str(payload.get("collection") or "")
        theory = str(payload.get("theory") or "").strip()
        method = str(payload.get("method") or "").strip()
        try:
            result = store.group(collection, theory, method)
        except ValueError as error:
            return jsonify({"error": str(error)}), 404
        try:
            content, _usage = ai._chat(_group_synthesis_prompt(result), system="Return only valid JSON, no markdown.")
            data = _parse_ai_json(content)
            data["generated_at"] = _utc_now()
            store.save_enrichment(_group_cache_key(collection, theory, method), "mapgroup", data)
            return jsonify({"synthesis": data})
        except (RuntimeError, requests.RequestException, ValueError) as error:
            return jsonify({"error": str(error)}), 502

    @app.get("/api/settings")
    def settings() -> Any:
        web_configured = bool(os.getenv("ZOTERO_USER_ID", "").strip() and os.getenv("ZOTERO_API_KEY", "").strip())
        if _use_zotero_web_api():
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
            return jsonify({"error": "找不到任务���"}), 404

    @app.post("/api/tasks/<task_id>/adopt")
    def adopt_task(task_id: str) -> Any:
        try:
            task_data = ai.get(task_id)
        except KeyError:
            return jsonify({"error": "找不到任务。"}), 404
        if task_data.get("type") != "read" or task_data.get("status") != "待采用" or not task_data.get("result"):
            return jsonify({"error": "只有完成的精读草稿可以采用。"}), 409
        paper = task_data.get("paper") or {}
        snapshot_info = task_data.get("snapshot") or {}
        snapshot_path = str(snapshot_info.get("temp_path") or snapshot_info.get("path") or "")
        snapshot_file = Path(snapshot_path) if Path(snapshot_path).is_absolute() else (base_dir / snapshot_path).resolve()
        allowed_roots = {base_dir, Path(tempfile.gettempdir()).resolve()}
        if not snapshot_file.is_file() or not any(root == snapshot_file.parent or root in snapshot_file.parents for root in allowed_roots):
            return jsonify({"error": "精读证据快照已不存在，无法采用。"}), 409
        snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
        document = card_document(paper, {**snapshot, "path": snapshot_info.get("path", snapshot.get("path", ""))}, str(task_data["result"]))
        metadata, body = split_front_matter(document)
        try:
            content, _usage = ai._chat(_theory_method_prompt(body), system="Return only valid JSON, no markdown.")
            extracted = _parse_ai_json(content)
            metadata["theories"] = [{"name": str(name)} for name in extracted.get("theories", []) if name][:4]
            metadata["methods"] = [{"name": str(name)} for name in extracted.get("methods", []) if name][:4]
            document = render_front_matter(metadata, body)
        except (RuntimeError, requests.RequestException, ValueError):
            pass  # 抽取失败时保留空标注，稍后可在卡片里手动补

        if database.available:
            stored_metadata = {**metadata, "_evidence_snapshot": snapshot}
            database.save_reading_card(
                str(paper["item_key"]),
                str(paper["collection"]),
                str(paper["citekey"]),
                stored_metadata,
                body,
                str(snapshot["attachment_key"]),
                str(snapshot["content_sha256"]),
            )
        else:
            output = store.cards_dir / (re.sub(r"[^A-Za-z0-9._-]", "-", str(paper["citekey"])).strip("-") or "untitled")
            output = output.with_suffix(".md")
            if output.exists():
                return jsonify({"error": "正式卡片已存在；请先查看并决定是否保留现有卡片。"}), 409
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(document, encoding="utf-8")
        snapshot_file.unlink(missing_ok=True)
        task_data["status"] = "已采用"
        ai._save_task(task_id)
        return jsonify(store.detail(str(paper["citekey"]), str(paper["collection"]))), 201

    @app.post("/api/cards/<citekey>/sync")
    def sync_card(citekey: str) -> Any:
        try:
            safe_citekey = _safe_id(citekey)
            requested_collection = request.args.get("collection")
            try:
                _, metadata, _ = store._find(safe_citekey, requested_collection)
                collection = str(metadata["collection"])
                report = sync_zotero_notes(store.cards_dir, collection, base_dir / "research" / "zotero-sync" / "notes.json", zotero_web_client_from_env(), dry_run=False)
            except KeyError:
                card = database.reading_card(safe_citekey, requested_collection) if database.available else None
                if not card:
                    raise
                metadata, body = dict(card["metadata"]), str(card["content"])
                collection = str(metadata["collection"])
                public_metadata = {key: value for key, value in metadata.items() if not key.startswith("_")}
                with tempfile.TemporaryDirectory(prefix="zotero-sync-") as temporary:
                    cards_dir = Path(temporary) / "cards"
                    cards_dir.mkdir()
                    (cards_dir / f"{safe_citekey}.md").write_text(render_front_matter(public_metadata, body), encoding="utf-8")
                    report = sync_zotero_notes(cards_dir, collection, Path(temporary) / "notes.json", zotero_web_client_from_env(), dry_run=False)
            result = next((item for item in report if Path(str(item.get("card_path", ""))).name == f"{safe_citekey}.md"), None)
            if not result or result.get("status") not in {"created", "updated", "unchanged"}:
                raise ResearchWorkspaceError(str((result or {}).get("issues", ["同步未完成"])[0]))
            store.update_sync_metadata(safe_citekey, collection, result.get("note_key"))
            return jsonify(result)
        except (KeyError, ResearchWorkspaceError, DatabaseUnavailable) as error:
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
