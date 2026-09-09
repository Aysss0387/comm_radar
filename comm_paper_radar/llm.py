"""OpenAI-compatible LLM client for daily deep-reading analyses.

Uses only public metadata (title, authors, venue, abstract, keyword hits,
links) so it stays inside the project's compliance boundary. Missing key or
request failure degrades gracefully: callers get None and render rule-based
reasons instead.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import requests

from .models import Paper


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

ANALYSIS_FIELDS = ("why_read", "rq_theory", "data_method", "findings", "focus_section", "takeaways")

SYSTEM_PROMPT = (
    "你是一位传播学博士生的精读助理。你只能依据提供的公开元数据（标题、作者、期刊、摘要）作分析，"
    "不得虚构论文内容。输出必须是合法 JSON。"
)

PROMPT_TEMPLATE = """请为下面这篇论文生成结构化精读解读，读者是研究网络诈骗受害叙事、framing 与人机传播的传播学博士生。

论文元数据：
- 标题：{title}
- 作者：{authors}
- 期刊：{venue}
- 发表日期：{publication_date}
- DOI：{doi}
- 入选槽位：{slot_label}
- 命中关键词：{keywords}
- 摘要：{abstract}

{abstract_note}

请输出 JSON（不要输出其他内容），字段如下：
{{
  "why_read": "为什么值得读（2-3 句，针对这位读者）",
  "rq_theory": "研究问题与理论框架（若无摘要则写明是推断）",
  "data_method": "数据与方法（若无摘要则写明是推断）",
  "findings": "核心发现（若无摘要则写明是推断）",
  "focus_section": "最值得精读哪一部分及原因",
  "takeaways": "这位读者自己的研究能直接借鉴什么思路（具体到可操作）",
  "priority": 1 到 5 的整数（5 = 必须精读）,
  "inferred": true 或 false（是否主要基于标题与期刊推断）
}}"""

NO_ABSTRACT_NOTE = (
    "注意：这篇论文没有公开摘要。你只能基于标题、期刊与关键词做出推断，"
    "请把 inferred 设为 true，并在各字段中明确标注“（推断）”。"
)


class LLMClient:
    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", timeout: int = 120):
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL", "") or DEFAULT_MODEL
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def deep_read(self, paper: Paper, slot_label: str) -> Optional[Dict[str, Any]]:
        """Return a structured analysis dict, or None when unavailable."""
        if not self.configured:
            return None
        abstract = paper.abstract.strip()
        prompt = PROMPT_TEMPLATE.format(
            title=paper.title,
            authors="; ".join(paper.authors[:8]) or "未知",
            venue=paper.venue or "未知",
            publication_date=paper.publication_date or paper.year or "未知",
            doi=paper.doi or "无",
            slot_label=slot_label,
            keywords="、".join(paper.keywords_hit[:8]) or "无",
            abstract=abstract[:2400] if abstract else "（无公开摘要）",
            abstract_note="" if abstract else NO_ABSTRACT_NOTE,
        )
        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                },
                timeout=self.timeout,
            )
        except requests.RequestException:
            return None
        if not response.ok:
            return None
        try:
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except (ValueError, IndexError):
            return None
        return parse_analysis(content, has_abstract=bool(abstract))


def parse_analysis(content: str, has_abstract: bool) -> Optional[Dict[str, Any]]:
    """Parse the model's JSON output; tolerate markdown fences."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    analysis: Dict[str, Any] = {}
    for field_name in ANALYSIS_FIELDS:
        analysis[field_name] = str(payload.get(field_name, "")).strip()
    if not analysis["why_read"]:
        return None
    try:
        priority = int(payload.get("priority", 3))
    except (TypeError, ValueError):
        priority = 3
    analysis["priority"] = max(1, min(5, priority))
    analysis["inferred"] = bool(payload.get("inferred", not has_abstract))
    return analysis
