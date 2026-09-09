"""Evidence-backed reading drafts sourced from Zotero's local full-text index."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from pypdf import PdfReader

from .research_workspace import (
    BetterBibTeXClient,
    ResearchWorkspaceError,
    ZoteroLocalClient,
    bbt_planned_citekey,
    render_front_matter,
    zotero_web_client_from_env,
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
    """Creates immutable evidence snapshots from Desktop or Zotero Cloud."""

    def __init__(self, base_dir: Path, cloud: bool = False) -> None:
        self.base_dir = base_dir
        self.cloud = cloud
        self.cache_dir = (Path(tempfile.gettempdir()) if cloud else base_dir) / "research" / "cache" / "fulltext"
        self.drafts_dir = base_dir / "research" / "drafts" / "reading"

    def material(self, item_key: str) -> Dict[str, Any]:
        zotero = zotero_web_client_from_env() if self.cloud else ZoteroLocalClient()
        item = zotero.item(item_key)
        data = item.get("data", {})
        attachments: List[Dict[str, Any]] = []
        for attachment in zotero.child_attachments(item_key):
            attachment_data = attachment.get("data", {})
            key = str(attachment.get("key") or attachment_data.get("key") or "")
            if not key:
                continue
            content_type = str(attachment_data.get("contentType") or "")
            link_mode = str(attachment_data.get("linkMode") or "")
            if self.cloud:
                indexed = content_type.lower() == "application/pdf" and link_mode in {"imported_file", "imported_url"}
            else:
                try:
                    indexed = bool(str(zotero.fulltext(key).get("content") or "").strip())
                except ResearchWorkspaceError:
                    indexed = False
            attachments.append({
                "key": key,
                "name": attachment_data.get("filename") or attachment_data.get("title") or key,
                "content_type": content_type,
                "link_mode": link_mode,
                "indexed": indexed,
            })
        citekey = (data.get("citationKey") or bbt_planned_citekey(item)) if self.cloud else BetterBibTeXClient().citationkeys([item_key]).get(item_key, item_key)
        return {
            "item_key": item_key,
            "citekey": citekey,
            "title": data.get("title") or "未命名条目",
            "year": str(data.get("date") or "")[:4],
            "attachments": sorted(attachments, key=lambda entry: (not entry["indexed"], not entry["name"].lower().endswith(".pdf"))),
        }

    @staticmethod
    def _extract_pdf(content: bytes, max_pages: int = 80, max_characters: int = 180_000) -> str:
        path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(content)
                path = Path(handle.name)
            reader = PdfReader(str(path))
            pieces: List[str] = []
            length = 0
            for page_number, page in enumerate(reader.pages[:max_pages], start=1):
                page_text = str(page.extract_text() or "").strip()
                if not page_text:
                    continue
                section = f"\n\n[PDF 第 {page_number} 页]\n{page_text}"
                remaining = max_characters - length
                if remaining <= 0:
                    break
                pieces.append(section[:remaining])
                length += len(pieces[-1])
            text = "".join(pieces).strip()
        except Exception as error:
            raise ResearchWorkspaceError(f"PDF 解析失败：{error}") from error
        finally:
            if path:
                path.unlink(missing_ok=True)
        if not text:
            raise ResearchWorkspaceError("PDF 没有可提取文本，可能是扫描件；请先在 Zotero 或 OCR 工具中完成文字识别。")
        return text

    def snapshot(self, paper: Mapping[str, Any], attachment_key: str, chunk_size: int = 6000) -> Dict[str, Any]:
        if self.cloud:
            content = self._extract_pdf(zotero_web_client_from_env().download_pdf(attachment_key))
        else:
            content = str(ZoteroLocalClient().fulltext(attachment_key).get("content") or "").strip()
        if not content:
            raise ResearchWorkspaceError("所选附件没有可用全文，无法生成全文精读。")
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
        payload["temp_path"] = str(path)
        payload["path"] = f"zotero-cloud:{attachment_key}#{digest[:16]}" if self.cloud else str(path.relative_to(self.base_dir))
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
