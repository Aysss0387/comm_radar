"""Markdown-first helpers for the Zotero research workspace.

Cards are the source of truth. Better BibTeX provides citekeys and Zotero child
notes are an overwriteable rendered view of a card.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import markdown
import requests
import yaml


CARD_REQUIRED_FIELDS = ("citekey", "title", "collection")
MY_THOUGHTS_HEADING = "## My Thoughts"
EVIDENCE_PATTERN = re.compile(r"(?:¶\s*\d+|段落\s*\d+|paragraph\s*\d+)", re.IGNORECASE)
DEFAULT_ZOTERO_URL = "http://127.0.0.1:23119/api"
DEFAULT_BBT_URL = "http://127.0.0.1:23119/better-bibtex/json-rpc"
ZOTERO_WEB_API_URL = "https://api.zotero.org"
CITEKEY_FORMULA = 'auth.lower + "-" + year + "-" + shorttitle(3,3)'
NOTE_MARKER = "codex-research-card:v1"


class ResearchWorkspaceError(RuntimeError):
    """An expected local Zotero/BBT workflow failure."""


def collection_slug(name: str) -> str:
    value = re.sub(r"\s+", "-", name.strip())
    value = re.sub(r"[^\w\-\u4e00-\u9fff]", "-", value, flags=re.UNICODE)
    return re.sub(r"-{2,}", "-", value).strip("-").lower() or "collection"


def card_path(cards_dir: Path, citekey: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9._-]", "-", citekey).strip("-") or "untitled"
    return cards_dir / f"{safe_key}.md"


def render_card_template(citekey: str, title: str, collection: str, zotero_item_key: Optional[str] = None) -> str:
    metadata: Dict[str, Any] = {"citekey": citekey, "title": title, "collection": collection, "theories": [], "methods": []}
    if zotero_item_key:
        metadata["zotero_item_key"] = zotero_item_key
    item_link = f"zotero://select/library/items/{zotero_item_key}" if zotero_item_key else "未绑定"
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return f"""---
{front_matter}
---

# {title}

## Bibliographic Information

- Citekey: [{citekey}]
- Zotero collection: {collection}
- Zotero item: {item_link}

## Research Problem

未报告。

## Research Questions / Hypotheses

未报告。

## Theory and Key Constructs

未报告。

## Operationalization

未报告。

## Research Context, Data, and Sample

未报告。

## Method and Analysis

未报告。

## Main Findings

未报告。

## Contributions and Limitations

未报告。

## Key Evidence

- 在此记录可回溯的原文证据，例如：[{citekey} ¶12]。

{MY_THOUGHTS_HEADING}

"""


def split_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("证据卡必须以 YAML front matter 开始。")
    closing = text.find("\n---\n", 4)
    if closing == -1:
        raise ValueError("证据卡的 YAML front matter 未闭合。")
    metadata = yaml.safe_load(text[4:closing]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("证据卡 front matter 必须是对象。")
    return metadata, text[closing + 5 :]


def render_front_matter(metadata: Mapping[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(dict(metadata), allow_unicode=True, sort_keys=False).strip() + "\n---\n" + body


def load_card(path: Path) -> Tuple[Dict[str, Any], str]:
    return split_front_matter(path.read_text(encoding="utf-8"))


def extract_my_thoughts(text: str) -> str:
    marker = text.find(MY_THOUGHTS_HEADING)
    return "" if marker == -1 else text[marker + len(MY_THOUGHTS_HEADING) :]


def preserve_my_thoughts(existing: str, generated: str) -> str:
    notes = extract_my_thoughts(existing)
    if not notes.strip():
        return generated
    marker = generated.find(MY_THOUGHTS_HEADING)
    if marker == -1:
        return generated.rstrip() + f"\n\n{MY_THOUGHTS_HEADING}\n" + notes.lstrip()
    return generated[: marker + len(MY_THOUGHTS_HEADING)] + notes


def validate_card(path: Path) -> List[str]:
    try:
        metadata, body = load_card(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        return [str(error)]
    issues: List[str] = []
    for field in CARD_REQUIRED_FIELDS:
        if not str(metadata.get(field, "")).strip():
            issues.append(f"缺少 front matter 字段：{field}")
    for key in ("theories", "methods"):
        entries = metadata.get(key)
        if not isinstance(entries, list):
            issues.append(f"{key} 必须是列表。")
            continue
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                issues.append(f"{key} 第 {index} 项需要 name。")
                continue
            evidence = str(entry.get("evidence", "")).strip()
            if not evidence:
                issues.append(f"{key} 第 {index} 项需要 evidence。")
            elif not EVIDENCE_PATTERN.search(evidence):
                issues.append(f"{key} 第 {index} 项 evidence 需要段落定位（如 ¶12）。")
    if MY_THOUGHTS_HEADING not in body:
        issues.append(f"缺少 {MY_THOUGHTS_HEADING} 区域。")
    return issues


def iter_collection_cards(cards_dir: Path, collection: str) -> Iterable[Tuple[Path, Dict[str, Any]]]:
    for path in sorted(cards_dir.glob("*.md")):
        try:
            metadata, _ = load_card(path)
        except (ValueError, yaml.YAMLError):
            continue
        if metadata.get("collection") == collection:
            yield path, metadata


def _value(entry: Dict[str, Any], key: str) -> str:
    value = entry.get(key, "未报告")
    if isinstance(value, list):
        return "；".join(str(item) for item in value if str(item).strip()) or "未报告"
    return str(value).strip() or "未报告"


def _render_map(title: str, collection: str, groups: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]], columns: List[Tuple[str, str]]) -> str:
    lines = [f"# {title}", "", f"- Zotero 集合：{collection}", f"- 生成日期：{date.today().isoformat()}", "- 证据范围：仅包含本项目已验证的精读卡；“未报告”不表示原论文不存在该信息。", ""]
    if not groups:
        return "\n".join(lines + ["尚无可用于生成地图的精读卡元数据。", ""])
    for name in sorted(groups, key=str.casefold):
        headers = [label for _, label in columns] + ["支撑论文与证据"]
        lines.extend([f"## {name}", "", "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"])
        for metadata, entry in groups[name]:
            cells = [_value(entry, key).replace("|", "\\|") for key, _ in columns]
            evidence = _value(entry, "evidence").replace("|", "\\|")
            source = f"[{metadata.get('citekey', 'unknown')}] — {evidence}"
            lines.append("| " + " | ".join(cells + [source]) + " |")
        lines.append("")
    return "\n".join(lines)


def build_atlas(cards_dir: Path, collection: str, output_dir: Path) -> Tuple[Path, Path, int]:
    theory_groups: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = defaultdict(list)
    method_groups: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = defaultdict(list)
    valid_cards = 0
    for path, metadata in iter_collection_cards(cards_dir, collection):
        if validate_card(path):
            continue
        valid_cards += 1
        for entry in metadata["theories"]:
            theory_groups[_value(entry, "name")].append((metadata, entry))
        for entry in metadata["methods"]:
            method_groups[_value(entry, "name")].append((metadata, entry))
    output_dir.mkdir(parents=True, exist_ok=True)
    theory_path, method_path = output_dir / "theory-map.md", output_dir / "method-bank.md"
    theory_path.write_text(_render_map("理论地图", collection, theory_groups, [("definition", "定义"), ("dimensions", "维度"), ("operationalization", "操作化"), ("context", "研究语境"), ("outcomes", "结果变量")]), encoding="utf-8")
    method_path.write_text(_render_map("方法银行", collection, method_groups, [("design", "研究设计"), ("data_and_sample", "数据与样本"), ("analysis", "分析流程"), ("validation", "信度/验证/稳健性"), ("research_question", "适用问题")]), encoding="utf-8")
    return theory_path, method_path, valid_cards


def render_idea_audit_template(collection: str, question: str, card_count: int) -> str:
    return f"""# 证据驱动的 Idea Audit

- Zotero 集合：{collection}
- 研究问题：{question}
- 已纳入并通过校验的精读卡：{card_count} 篇
- 生成日期：{date.today().isoformat()}

## Evidence Coverage

说明本次检索集合、精读卡覆盖情况，以及未纳入的文献类型。以下结论仅适用于这一证据范围。

## What Is Known

按主题列出已有发现，每条均附 citekey 与段落证据。

## Research Design Distribution

按理论、对象/语境、方法、样本或平台整理分布；区分“未报告”与“未覆盖”。

## Tensions and Boundary Conditions

记录相反发现，并比较样本、国家/平台、测量和方法的差异。

## Candidate Gaps

每项必须说明：现有证据、低覆盖的组合、最近的竞争性研究、为何它只是候选 gap。

## Candidate Research Ideas

每项必须包含：已有研究、缺少什么、RQ/H、数据与方法建议、预期贡献、最近竞争性研究及证据定位。

## Limits

不得把本集合中未发现的文献表述为整个领域不存在。
"""


class BetterBibTeXClient:
    def __init__(self, url: str = DEFAULT_BBT_URL, session: Optional[Any] = None) -> None:
        self.url, self.session = url, session or requests.Session()

    def _call(self, method: str, params: List[Any]) -> Any:
        try:
            response = self.session.post(self.url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as error:
            raise ResearchWorkspaceError(f"无法连接 Better BibTeX：{error}") from error
        if payload.get("error"):
            raise ResearchWorkspaceError(f"Better BibTeX 返回错误：{payload['error'].get('message', payload['error'])}")
        return payload.get("result")

    def citationkeys(self, item_keys: List[str]) -> Dict[str, str]:
        return {str(k): str(v) for k, v in (self._call("item.citationkey", [item_keys]) or {}).items() if v}

    def regenerate_keys(self, citekeys: List[str]) -> Dict[str, Optional[str]]:
        return {str(k): (str(v) if v else None) for k, v in (self._call("item.regenerate_key", [citekeys]) or {}).items()}


class ZoteroLocalClient:
    def __init__(self, url: str = DEFAULT_ZOTERO_URL, session: Optional[Any] = None) -> None:
        self.url, self.session, self.last_version = url.rstrip("/"), session or requests.Session(), None
        self.server_id: Optional[str] = None

    def _request(self, method: str, path: str, **kwargs: Any) -> Tuple[Any, Mapping[str, str]]:
        try:
            response = self.session.request(method, f"{self.url}{path}", timeout=10, **kwargs)
        except requests.RequestException as error:
            raise ResearchWorkspaceError(f"无法连接 Zotero 本地 API：{error}") from error
        self.last_version = response.headers.get("X-Zotero-Version", self.last_version)
        self.server_id = response.headers.get("Zotero-Server-ID", self.server_id)
        if not response.ok:
            raise ResearchWorkspaceError(f"Zotero 本地 API 请求失败（{response.status_code}）：{response.text.strip()[:300] or response.reason}")
        try:
            return response.json(), response.headers
        except ValueError:
            return None, response.headers

    def collections(self) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", "/users/0/collections", params={"limit": 100})
        return list(payload or [])

    def collection_by_name(self, name: str) -> Dict[str, Any]:
        matches = [item for item in self.collections() if item.get("data", {}).get("name") == name]
        if len(matches) != 1:
            raise ResearchWorkspaceError(f"Zotero 中{('没有' if not matches else '有多个')}名为“{name}”的集合。")
        return matches[0]

    def collection_items(self, collection_key: str) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", f"/users/0/collections/{collection_key}/items", params={"include": "data", "limit": 100})
        return [item for item in (payload or []) if item.get("data", {}).get("itemType") not in {"attachment", "note", "annotation"} and not item.get("data", {}).get("parentItem")]

    def item(self, item_key: str) -> Dict[str, Any]:
        payload, _ = self._request("GET", f"/users/0/items/{item_key}", params={"include": "data"})
        return payload if isinstance(payload, dict) else {}

    def child_notes(self, parent_item_key: str) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", f"/users/0/items/{parent_item_key}/children", params={"include": "data", "limit": 100})
        return [item for item in (payload or []) if item.get("data", {}).get("itemType") == "note"]

    def child_attachments(self, parent_item_key: str) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", f"/users/0/items/{parent_item_key}/children", params={"include": "data", "limit": 100})
        return [item for item in (payload or []) if item.get("data", {}).get("itemType") == "attachment"]

    def fulltext(self, attachment_key: str) -> Dict[str, Any]:
        payload, _ = self._request("GET", f"/users/0/items/{attachment_key}/fulltext")
        return payload if isinstance(payload, dict) else {}


class ZoteroWebClient:
    """Zotero Web API client: the reliable write channel for child notes.

    The local API stays a read-only channel (attachments, fulltext). All note
    writes go through api.zotero.org so they survive local API limitations and
    show up after the desktop app syncs.
    """

    def __init__(self, user_id: str, api_key: str, base_url: str = ZOTERO_WEB_API_URL, session: Optional[Any] = None) -> None:
        if not str(user_id).strip() or not str(api_key).strip():
            raise ResearchWorkspaceError("缺少 ZOTERO_USER_ID 或 ZOTERO_API_KEY，无法使用 Zotero Web API。")
        self.base_url = base_url.rstrip("/")
        self.prefix = f"/users/{str(user_id).strip()}"
        self.api_key = str(api_key).strip()
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, version: Optional[int] = None, **kwargs: Any) -> Tuple[Any, Mapping[str, str]]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Zotero-API-Key", self.api_key)
        headers.setdefault("Zotero-API-Version", "3")
        if version is not None:
            headers["If-Unmodified-Since-Version"] = str(version)
        try:
            response = self.session.request(method, f"{self.base_url}{self.prefix}{path}", timeout=30, headers=headers, **kwargs)
        except requests.RequestException as error:
            raise ResearchWorkspaceError(f"无法连接 Zotero Web API：{error}") from error
        if response.status_code == 403:
            raise ResearchWorkspaceError("Zotero Web API 拒绝访问（403）：请确认 API Key 勾选了库的写权限（Allow write access）。")
        if response.status_code == 412:
            raise ResearchWorkspaceError("Zotero 云端笔记版本比本地记录更新（412）：请先在 Zotero 桌面端完成同步，再重试。")
        if not response.ok:
            raise ResearchWorkspaceError(f"Zotero Web API 请求失败（{response.status_code}）：{response.text.strip()[:300] or response.reason}")
        try:
            return response.json(), response.headers
        except ValueError:
            return None, response.headers

    def collections(self) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", "/collections", params={"limit": 100})
        return list(payload or [])

    def collection_by_name(self, name: str) -> Dict[str, Any]:
        matches = [item for item in self.collections() if item.get("data", {}).get("name") == name]
        if len(matches) != 1:
            raise ResearchWorkspaceError(f"Zotero 云端{('没有' if not matches else '有多个')}名为“{name}”的集合。")
        return matches[0]

    def collection_items(self, collection_key: str) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", f"/collections/{collection_key}/items", params={"limit": 100})
        return [item for item in (payload or []) if item.get("data", {}).get("itemType") not in {"attachment", "note", "annotation"} and not item.get("data", {}).get("parentItem")]

    def item(self, item_key: str) -> Dict[str, Any]:
        payload, _ = self._request("GET", f"/items/{item_key}")
        return payload if isinstance(payload, dict) else {}

    def child_notes(self, parent_item_key: str) -> List[Dict[str, Any]]:
        payload, _ = self._request("GET", f"/items/{parent_item_key}/children", params={"limit": 100})
        return [item for item in (payload or []) if item.get("data", {}).get("itemType") == "note"]

    def create_note(self, parent_item_key: str, note_html: str) -> Tuple[str, int]:
        payload, _ = self._request("POST", "/items", json=[{"itemType": "note", "parentItem": parent_item_key, "note": note_html}])
        result = (payload or {}).get("successful", {}).get("0") or (payload or {}).get("successful", {}).get(0)
        if not result or not result.get("key"):
            failed = (payload or {}).get("failed", {})
            raise ResearchWorkspaceError(f"Zotero Web API 未创建子 Note：{failed or payload}")
        return str(result["key"]), int(result.get("version", 0))

    def update_note(self, note_key: str, note_html: str, version: Optional[int]) -> int:
        if version is None:
            current = self.item(note_key)
            version = int(current.get("data", {}).get("version", current.get("version", 0)) or 0)
        _, response_headers = self._request("PATCH", f"/items/{note_key}", version=version, json={"note": note_html})
        return int(response_headers.get("Last-Modified-Version", version or 0))


def zotero_web_client_from_env(session: Optional[Any] = None) -> ZoteroWebClient:
    user_id = os.getenv("ZOTERO_USER_ID", "").strip()
    api_key = os.getenv("ZOTERO_API_KEY", "").strip()
    if not user_id or not api_key:
        raise ResearchWorkspaceError(
            "未配置 Zotero Web API：请在 https://www.zotero.org/settings/keys 创建带写权限的 API Key，"
            "然后设置环境变量 ZOTERO_USER_ID（个人库 userID，可在同一页面查看）和 ZOTERO_API_KEY。"
        )
    return ZoteroWebClient(user_id, api_key, session=session)


def _item_key(item: Mapping[str, Any]) -> str:
    return str(item.get("key") or item.get("data", {}).get("key") or "")


def _item_tags(item: Mapping[str, Any]) -> List[str]:
    return [str(tag.get("tag", "")) for tag in item.get("data", {}).get("tags", []) if isinstance(tag, dict)]


def bbt_planned_citekey(item: Mapping[str, Any]) -> str:
    """Predict the agreed formula; Better BibTeX remains the final authority."""
    data = item.get("data", {})
    creator = next((entry for entry in data.get("creators", []) if entry.get("lastName") or entry.get("name")), {})
    author = str(creator.get("lastName") or creator.get("name") or "unknown").lower()
    year_match = re.search(r"\d{4}", str(data.get("date", "")))
    words = re.findall(r"[\wÀ-ÖØ-öø-ÿ]+", str(data.get("title", "")), flags=re.UNICODE)[:3]
    short_title = "".join(word[:1].upper() + word[1:] for word in words) or "Untitled"
    plain_author = "".join(char for char in unicodedata.normalize("NFKD", author) if not unicodedata.combining(char))
    plain_author = re.sub(r"[^a-z0-9]+", "", plain_author) or "unknown"
    return f"{plain_author}-{year_match.group(0) if year_match else 'nd'}-{short_title}"


def generate_citekey_preview(collection: str, cards_dir: Path, zotero: ZoteroLocalClient, bbt: BetterBibTeXClient, tag: str = "⭐⭐⭐⭐⭐") -> Dict[str, Any]:
    zotero_collection = zotero.collection_by_name(collection)
    items = [item for item in zotero.collection_items(_item_key(zotero_collection)) if tag in _item_tags(item)]
    current = bbt.citationkeys([_item_key(item) for item in items]) if items else {}
    cards = {str(meta.get("zotero_item_key") or meta.get("citekey")): path for path, meta in iter_collection_cards(cards_dir, collection)}
    collisions: Dict[str, List[str]] = defaultdict(list)
    entries: List[Dict[str, Any]] = []
    for item in items:
        item_key, planned = _item_key(item), bbt_planned_citekey(item)
        collisions[planned.casefold()].append(item_key)
        source = cards.get(item_key)
        data = item.get("data", {})
        has_creator = any(entry.get("lastName") or entry.get("name") for entry in data.get("creators", []))
        has_year = bool(re.search(r"\d{4}", str(data.get("date", ""))))
        status = "ready" if source and current.get(item_key) and has_creator and has_year else "needs-review"
        entries.append({"zotero_item_key": item_key, "title": data.get("title", ""), "old_citekey": current.get(item_key), "planned_citekey": planned, "card_path": str(source) if source else None, "status": status, "issues": ([] if status == "ready" else ["缺少卡片、现有 citekey、第一作者或年份；请先在 Zotero 补全元数据。"])})
    duplicates = {key: keys for key, keys in collisions.items() if len(keys) > 1}
    for entry in entries:
        if entry["planned_citekey"].casefold() in duplicates:
            entry["status"] = "conflict"
    return {"schema_version": 1, "collection": collection, "tag": tag, "formula": CITEKEY_FORMULA, "generated_at": datetime.now(timezone.utc).isoformat(), "note": "planned_citekey 为本公式预估值；Better BibTeX 会处理转写和重名后缀，实际重新生成返回值才是最终值。", "entries": entries, "conflicts": duplicates}


def write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def migrate_citekeys(preview: Mapping[str, Any], cards_dir: Path, bbt: BetterBibTeXClient, registry_path: Path) -> Dict[str, str]:
    if preview.get("conflicts"):
        raise ResearchWorkspaceError("迁移预览存在 planned_citekey 冲突，不能执行。")
    entries = list(preview.get("entries", []))
    if not entries or any(entry.get("status") != "ready" for entry in entries):
        raise ResearchWorkspaceError("迁移预览中有缺少卡片或 citekey 的条目，不能执行。")
    old_keys = [str(entry["old_citekey"]) for entry in entries]
    raw_mapping = bbt.regenerate_keys(old_keys)
    if any(not raw_mapping.get(key) for key in old_keys):
        raise ResearchWorkspaceError("Better BibTeX 未能重新生成全部 citekey；Markdown 尚未改动。")
    mapping = {key: str(raw_mapping[key]) for key in old_keys}
    targets: Dict[Path, Path] = {}
    for entry in entries:
        source = Path(str(entry["card_path"])); target = card_path(cards_dir, mapping[str(entry["old_citekey"])])
        if not source.exists():
            raise ResearchWorkspaceError(f"找不到待迁移卡片：{source}")
        if target.exists() and target != source:
            raise ResearchWorkspaceError(f"目标卡片已存在，拒绝覆盖：{target}")
        targets[source] = target
    for entry in entries:
        old, source = str(entry["old_citekey"]), Path(str(entry["card_path"]))
        metadata, body = load_card(source)
        metadata["citekey"], metadata["zotero_item_key"] = mapping[old], str(entry["zotero_item_key"])
        target = targets[source]
        target.write_text(render_front_matter(metadata, body.replace(old, mapping[old])), encoding="utf-8")
        if target != source:
            source.unlink()
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {"schema_version": 1, "notes": {}}
    for record in registry.get("notes", {}).values():
        if record.get("citekey") in mapping:
            record["citekey"] = mapping[record["citekey"]]
            record["card_path"] = str(card_path(cards_dir, record["citekey"]))
    write_json(registry_path, registry)
    return mapping


def render_zotero_note(path: Path, metadata: Mapping[str, Any], body: str) -> str:
    parent_key = str(metadata.get("zotero_item_key", "")).strip()
    if not parent_key:
        raise ResearchWorkspaceError(f"卡片缺少 zotero_item_key，无法同步：{path}")
    marker = f"<!-- {NOTE_MARKER} parent={parent_key} card={path.name} -->"
    return f'{marker}<h1>Codex 精读卡</h1><p><strong>{html.escape(str(metadata.get("title", path.stem)))}</strong><br>citekey: {html.escape(str(metadata.get("citekey", "")))}<br><a href="zotero://select/library/items/{html.escape(parent_key)}">在 Zotero 中打开条目</a></p><p><em>本笔记由 Codex 管理。请在本条目下新建独立笔记记录个人想法；直接修改本笔记会在同步前要求你处理冲突。</em></p>' + markdown.markdown(body.lstrip(), extensions=["tables", "sane_lists"])


def sync_zotero_notes(cards_dir: Path, collection: str, registry_path: Path, zotero: ZoteroLocalClient, dry_run: bool = True) -> List[Dict[str, Any]]:
    registry: Dict[str, Any] = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {"schema_version": 1, "notes": {}}
    registry.setdefault("notes", {})
    report: List[Dict[str, Any]] = []
    for path, metadata in iter_collection_cards(cards_dir, collection):
        issues = validate_card(path)
        if issues:
            report.append({"card_path": str(path), "status": "invalid", "issues": issues}); continue
        if not str(metadata.get("zotero_item_key", "")).strip():
            report.append({"card_path": str(path), "status": "unbound", "issues": ["缺少 zotero_item_key；请先完成 citekey 迁移或在卡片中补充父条目 key。"]}); continue
        _, body = load_card(path)
        note_html, parent_key = render_zotero_note(path, metadata, body), str(metadata["zotero_item_key"])
        digest, existing = hashlib.sha256(note_html.encode("utf-8")).hexdigest(), registry["notes"].get(parent_key, {})
        if existing.get("content_sha256") == digest:
            report.append({"card_path": str(path), "status": "unchanged", "note_key": existing.get("note_key")}); continue
        if dry_run:
            report.append({"card_path": str(path), "status": "would-update" if existing.get("note_key") else "would-create", "parent_item_key": parent_key}); continue
        note_key, version = existing.get("note_key"), existing.get("note_version")
        if note_key:
            note_version, status = zotero.update_note(str(note_key), note_html, int(version) if version else None), "updated"
        else:
            managed = next((note for note in zotero.child_notes(parent_key) if NOTE_MARKER in str(note.get("data", {}).get("note", ""))), None)
            if managed:
                note_key = _item_key(managed); note_version, status = zotero.update_note(note_key, note_html, int(managed.get("data", {}).get("version", 0) or 0)), "updated"
            else:
                note_key, note_version, status = *zotero.create_note(parent_key, note_html), "created"
        registry["notes"][parent_key] = {"note_key": note_key, "note_version": note_version, "citekey": metadata["citekey"], "card_path": str(path), "content_sha256": digest, "synced_at": datetime.now(timezone.utc).isoformat()}
        report.append({"card_path": str(path), "status": status, "note_key": note_key})
    if not dry_run:
        write_json(registry_path, registry)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Zotero 日常科研 Markdown 工作台")
    commands = parser.add_subparsers(dest="command", required=True)
    card = commands.add_parser("card-template", help="创建或更新精读卡模板")
    card.add_argument("--citekey", required=True); card.add_argument("--title", required=True); card.add_argument("--collection", required=True); card.add_argument("--zotero-item-key"); card.add_argument("--cards-dir", default="research/cards")
    validate = commands.add_parser("validate-card", help="校验证据卡"); validate.add_argument("path")
    atlas = commands.add_parser("build-atlas", help="从精读卡生成理论地图与方法银行"); atlas.add_argument("--collection", required=True); atlas.add_argument("--cards-dir", default="research/cards"); atlas.add_argument("--output-dir")
    audit = commands.add_parser("idea-template", help="创建 idea audit 模板"); audit.add_argument("--collection", required=True); audit.add_argument("--question", required=True); audit.add_argument("--cards-dir", default="research/cards"); audit.add_argument("--output", required=True)
    preview = commands.add_parser("citekey-preview", help="生成只读 citekey 迁移预览")
    preview.add_argument("--collection", required=True); preview.add_argument("--tag", default="⭐⭐⭐⭐⭐"); preview.add_argument("--cards-dir", default="research/cards"); preview.add_argument("--output"); preview.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL); preview.add_argument("--bbt-url", default=DEFAULT_BBT_URL)
    migrate = commands.add_parser("migrate-citekeys", help="确认后迁移 citekey 与 Markdown 卡片")
    migrate.add_argument("--preview", required=True); migrate.add_argument("--cards-dir", default="research/cards"); migrate.add_argument("--registry", default="research/zotero-sync/registry.json"); migrate.add_argument("--bbt-url", default=DEFAULT_BBT_URL); migrate.add_argument("--confirm", action="store_true", help="确认已审核预览并允许 Better BibTeX 改写 citekey")
    sync = commands.add_parser("sync-zotero-notes", help="将卡片同步为 Zotero 子 Note（写入走 Zotero Web API）")
    sync.add_argument("--collection", required=True); sync.add_argument("--cards-dir", default="research/cards"); sync.add_argument("--registry", default="research/zotero-sync/registry.json"); sync.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL); sync.add_argument("--apply", action="store_true", help="实际写入 Zotero；默认只显示预览")
    args = parser.parse_args(argv)
    if args.command == "card-template":
        output = card_path(Path(args.cards_dir), args.citekey); generated = render_card_template(args.citekey, args.title, args.collection, args.zotero_item_key)
        if output.exists(): generated = preserve_my_thoughts(output.read_text(encoding="utf-8"), generated)
        output.parent.mkdir(parents=True, exist_ok=True); output.write_text(generated, encoding="utf-8"); print(output); return 0
    if args.command == "validate-card":
        issues = validate_card(Path(args.path)); print("valid" if not issues else "\n".join(f"- {issue}" for issue in issues)); return 0 if not issues else 1
    if args.command == "build-atlas":
        output_dir = Path(args.output_dir) if args.output_dir else Path("research/collections") / collection_slug(args.collection)
        theory, methods, count = build_atlas(Path(args.cards_dir), args.collection, output_dir); print(f"Processed {count} valid cards\n{theory}\n{methods}"); return 0
    if args.command == "idea-template":
        matching = [path for path, _ in iter_collection_cards(Path(args.cards_dir), args.collection) if not validate_card(path)]
        output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(render_idea_audit_template(args.collection, args.question, len(matching)), encoding="utf-8"); print(output); return 0
    if args.command == "citekey-preview":
        output = Path(args.output) if args.output else Path("research/zotero-sync") / f"{collection_slug(args.collection)}-citekey-preview.json"
        write_json(output, generate_citekey_preview(args.collection, Path(args.cards_dir), ZoteroLocalClient(args.zotero_url), BetterBibTeXClient(args.bbt_url), args.tag)); print(output); return 0
    if args.command == "migrate-citekeys":
        if not args.confirm:
            print("为保护既有引用，migrate-citekeys 需要明确传入 --confirm。"); return 2
        mapping = migrate_citekeys(json.loads(Path(args.preview).read_text(encoding="utf-8")), Path(args.cards_dir), BetterBibTeXClient(args.bbt_url), Path(args.registry)); print(json.dumps(mapping, ensure_ascii=False, indent=2)); return 0
    zotero_client = zotero_web_client_from_env() if args.apply else ZoteroLocalClient(args.zotero_url)
    report = sync_zotero_notes(Path(args.cards_dir), args.collection, Path(args.registry), zotero_client, dry_run=not args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
