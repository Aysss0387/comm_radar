"""Evidence-backed reading drafts sourced from Zotero's local full-text index."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .research_workspace import (
    BetterBibTeXClient,
    ResearchWorkspaceError,
    ZoteroLocalClient,
    render_front_matter,
)


SECTION_ORDER = (
    "阅读概览",
    "研究问题",
    "理论与概念",
    "操作化",
    "数据与样本",
    "方法",
    "主要发现",
    "贡献与局限",
    "原文证据",
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReadingMaterialStore:
    """Creates immutable local snapshots so evidence remains inspectable."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.cache_dir = base_dir / "research" / "cache" / "fulltext"
        self.drafts_dir = base_dir / "research" / "drafts" / "reading"

    def material(self, item_key: str) -> Dict[str, Any]:
        zotero = ZoteroLocalClient()
        item = zotero.item(item_key)
        data = item.get("data", {})
        attachments: List[Dict[str, Any]] = []
        for attachment in zotero.child_attachments(item_key):
            attachment_data = attachment.get("data", {})
            key = str(attachment.get("key") or attachment_data.get("key") or "")
            if not key:
                continue
            try:
                indexed = bool(str(zotero.fulltext(key).get("content") or "").strip())
            except ResearchWorkspaceError:
                indexed = False
            attachments.append({
                "key": key,
                "name": attachment_data.get("filename") or attachment_data.get("title") or key,
                "content_type": attachment_data.get("contentType") or "",
                "indexed": indexed,
            })
        citekey = BetterBibTeXClient().citationkeys([item_key]).get(item_key, item_key)
        return {
            "item_key": item_key,
            "citekey": citekey,
            "title": data.get("title") or "未命名条目",
            "year": str(data.get("date") or "")[:4],
            "attachments": sorted(attachments, key=lambda entry: (not entry["indexed"], not entry["name"].lower().endswith(".pdf"))),
        }

    def snapshot(self, paper: Mapping[str, Any], attachment_key: str, chunk_size: int = 6000) -> Dict[str, Any]:
        zotero = ZoteroLocalClient()
        content = str(zotero.fulltext(attachment_key).get("content") or "").strip()
        if not content:
            raise ResearchWorkspaceError("所选附件没有 Zotero 已索引的全文，无法生成全文精读。")
        digest = _sha(content)
        blocks = []
        for start in range(0, len(content), chunk_size):
            end = min(len(content), start + chunk_size)
            blocks.append({"id": f"E{len(blocks) + 1:03d}", "start": start, "end": end, "text": content[start:end]})
        payload = {
            "schema_version": 1,
            "item_key": paper["item_key"],
            "attachment_key": attachment_key,
            "content_sha256": digest,
            "title": paper["title"],
            "chunks": blocks,
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{attachment_key}-{digest[:16]}.json"
        if not path.exists():
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["path"] = str(path.relative_to(self.base_dir))
        return payload

    def draft_path(self, task_id: str) -> Path:
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        return self.drafts_dir / f"{task_id}.md"


def reading_prompt(paper: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    evidence = "\n\n".join(f"【{chunk['id']}】\n{chunk['text']}" for chunk in snapshot["chunks"])
    headings = "\n".join(f"## {name}" for name in SECTION_ORDER)
    return f"""你是严谨的传播学研究助理。只根据下方给出的论文全文生成中文精读草稿。

论文：{paper['title']}（citekey: {paper['citekey']}）
必须使用以下章节，且每个事实性判断末尾附一个或多个证据编号，例如【E003】。没有证据时写“本次材料未覆盖”或“作者未报告”，不要猜测。不要把你的综合判断写成作者发现。

{headings}

在“原文证据”中列出支撑最重要结论的短引文与证据编号。不要使用 Markdown 表格。

全文：
{evidence}"""


def normalize_draft(result: str, paper: Mapping[str, Any]) -> str:
    text = result.strip()
    if not text.startswith("# "):
        text = f"# {paper['title']}\n\n" + text
    for heading in SECTION_ORDER:
        if f"## {heading}" not in text:
            text += f"\n\n## {heading}\n\n本次材料未覆盖。"
    if "## My Thoughts" not in text:
        text += "\n\n## My Thoughts\n\n"
    return text + "\n"


def card_document(paper: Mapping[str, Any], snapshot: Mapping[str, Any], draft: str) -> str:
    metadata = {
        "citekey": paper["citekey"],
        "title": paper["title"],
        "collection": paper["collection"],
        "zotero_item_key": paper["item_key"],
        "reading_type": "全文精读",
        "evidence_file": snapshot["path"],
        "evidence_status": "来源待核对",
        "sync_status": "未同步",
        "theories": [],
        "methods": [],
    }
    return render_front_matter(metadata, normalize_draft(draft, paper))


EVIDENCE_ID_RE = re.compile(r"【(E\d{3})】")


def evidence_ids(text: str) -> List[str]:
    return list(dict.fromkeys(EVIDENCE_ID_RE.findall(text)))
