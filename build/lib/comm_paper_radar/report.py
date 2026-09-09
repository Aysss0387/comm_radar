from pathlib import Path
from html import unescape
from typing import Iterable, List

from .models import Paper
from .storage import ensure_parent
from .utils import week_id


def write_report(path: Path, papers: List[Paper], title: str, mode: str = "weekly") -> None:
    ensure_parent(path)
    content = render_report(papers, title=title, mode=mode)
    path.write_text(content, encoding="utf-8")


def render_report(papers: List[Paper], title: str, mode: str = "weekly") -> str:
    domestic_count = len([paper for paper in papers if paper.region == "domestic"])
    international_count = len(papers) - domestic_count
    lines = [
        f"# {title}",
        "",
        f"- 生成日期：{__import__('datetime').date.today().isoformat()}",
        f"- 报告类型：{'自动周报' if mode == 'weekly' else '手动检索报告'}",
        f"- 入选论文：{len(papers)} 篇（国内 {domestic_count}，国外 {international_count}）",
        "- 合规说明：仅整理公开元数据、摘要、DOI、期刊页与 OA 链接，不下载全文。",
        "",
        "## 推荐列表",
        "",
    ]
    for rank, paper in enumerate(papers, start=1):
        lines.extend(render_paper(rank, paper))
    return "\n".join(lines).rstrip() + "\n"


def render_paper(rank: int, paper: Paper) -> List[str]:
    doi_url = f"https://doi.org/{paper.doi}" if paper.doi else ""
    links = []
    if doi_url:
        links.append(f"[DOI]({doi_url})")
    if paper.openalex_url:
        links.append(f"[OpenAlex]({paper.openalex_url})")
    if paper.publisher_url:
        links.append(f"[期刊页]({paper.publisher_url})")
    if paper.oa_url:
        links.append(f"[OA链接]({paper.oa_url})")
    topic = "、".join(paper.topic_labels) if paper.topic_labels else "传播学/计算传播"
    hits = "、".join(paper.keywords_hit[:8]) if paper.keywords_hit else "未记录"
    abstract = unescape(paper.abstract.strip().replace("\n", " "))
    if len(abstract) > 900:
        abstract = abstract[:900].rstrip() + "..."
    reason = build_reason(paper)
    return [
        f"### {rank}. {paper.title}",
        "",
        f"- 区域：{'国内' if paper.region == 'domestic' else '国外'}",
        f"- 主题标签：{topic}",
        f"- 作者：{'; '.join(paper.authors[:8]) or '未知'}",
        f"- 来源：{paper.venue or '未知'}，{paper.publication_date or paper.year or '日期未知'}",
        f"- 评分：总分 {paper.total_score:.2f}；权威 {paper.authority_score:.2f}，关键词 {paper.keyword_score:.2f}，引用 {paper.citation_score:.2f}，时效 {paper.recency_score:.2f}",
        f"- 命中关键词：{hits}",
        f"- 推荐理由：{reason}",
        f"- 链接：{' | '.join(links) if links else '暂无'}",
        "",
        f"摘要：{abstract or '暂无公开摘要（已尝试 OpenAlex、Crossref、Semantic Scholar 三个来源）。'}",
        "",
    ]


def build_reason(paper: Paper) -> str:
    pieces = []
    if paper.authority_score >= 90:
        pieces.append("来源权威")
    elif paper.authority_score >= 75:
        pieces.append("来源质量较高")
    if any("反诈" in label or "诈骗" in label for label in paper.topic_labels):
        pieces.append("紧扣反诈/风险传播重点")
    if any("人工智能" in label or "AI" in label for label in paper.topic_labels):
        pieces.append("覆盖人工智能与传播交叉议题")
    if paper.keyword_score >= 45:
        pieces.append("关键词相关度高")
    if paper.citation_score >= 60:
        pieces.append("已有较强引用影响")
    if paper.recency_score >= 80:
        pieces.append("时效性强")
    return "；".join(pieces) + "。" if pieces else "综合评分靠前，适合纳入本期跟踪。"


def default_weekly_report_path(base: Path) -> Path:
    return base / "reports" / f"{week_id()}.md"
