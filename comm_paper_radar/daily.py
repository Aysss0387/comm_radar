"""Daily three-slot deep-reading feed.

Every day picks exactly three papers, one per slot:
  1. relevance — closest to the user's current research (scams, framing, HMC)
  2. theory    — a theoretical framework or argument worth imitating
  3. method    — computational / methodological frontier

This is deliberately NOT an impact-factor top-3: each slot has its own signal
terms, tier-1/tier-2 journals get slot-appropriate bonuses, and feedback from
the workbench lightly re-weights future picks.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .llm import LLMClient
from .models import Paper
from .pipeline import collect_candidates, prepare_candidates
from .scoring import venue_meta
from .sources import SourceClient
from .storage import ensure_parent, load_recommended_keys
from .utils import normalize_text, today_iso


SLOT_ORDER = ("relevance", "theory", "method")
SLOT_FALLBACK_LABELS = {"relevance": "当前研究最相关", "theory": "理论值得学", "method": "方法/前沿"}


def daily_feed_path(base_dir: Path) -> Path:
    return base_dir / "data" / "daily_feed.jsonl"


def feedback_path(base_dir: Path) -> Path:
    return base_dir / "data" / "feedback.json"


def daily_report_path(base_dir: Path, day: str) -> Path:
    return base_dir / "reports" / "daily" / f"{day}.md"


def load_feed_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def load_feed_keys(path: Path) -> Set[str]:
    return {str(record.get("dedupe_key", "")) for record in load_feed_records(path) if record.get("dedupe_key")}


def load_feedback_weights(path: Path) -> Dict[str, Dict[str, int]]:
    """Aggregate useful/useless counts per venue for light re-weighting."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    venues = payload.get("venues", {})
    return venues if isinstance(venues, dict) else {}


def feedback_adjustment(venue: str, weights: Dict[str, Dict[str, int]]) -> float:
    """Light-touch adjustment: each net useful/useless vote moves the score by 2, capped at ±10."""
    venue_norm = normalize_text(venue)
    for name, counts in weights.items():
        if normalize_text(name) == venue_norm and isinstance(counts, dict):
            net = int(counts.get("useful", 0)) - int(counts.get("useless", 0))
            return float(max(-10, min(10, net * 2)))
    return 0.0


def slot_hits(paper: Paper, terms: List[str]) -> Tuple[float, List[str]]:
    title = normalize_text(paper.title)
    abstract = normalize_text(paper.abstract)
    score = 0.0
    hits: List[str] = []
    for term in terms:
        term_norm = normalize_text(term)
        if not term_norm:
            continue
        if term_norm in title:
            score += 10
            hits.append(term)
        elif term_norm in abstract:
            score += 5
            hits.append(term)
    return score, hits[:8]


def slot_score(paper: Paper, slot: str, terms: List[str], settings: Dict[str, Any], weights: Dict[str, Dict[str, int]]) -> Tuple[float, List[str]]:
    base, hits = slot_hits(paper, terms)
    if base <= 0:
        return 0.0, hits
    meta = venue_meta(paper.venue, settings)
    group = str(meta.get("group", ""))
    bonus = 0.0
    if slot == "theory" and group in {"tier_1", "tier_2"}:
        bonus += 15.0
    if slot == "method":
        if group == "ai_computation":
            bonus += 15.0
        if "communication methods and measures" in normalize_text(paper.venue):
            bonus += 20.0
    if slot == "relevance" and group in {"tier_1", "tier_2"}:
        bonus += 8.0
    return base + paper.total_score * 0.5 + bonus + feedback_adjustment(paper.venue, weights), hits


def select_daily(
    candidates: List[Paper],
    settings: Dict[str, Any],
    excluded_keys: Set[str],
    weights: Dict[str, Dict[str, int]],
) -> List[Tuple[str, Paper, List[str]]]:
    """Assign one paper per slot; avoid duplicate papers and (best effort) duplicate venues."""
    daily_config = settings.get("daily", {}) or {}
    slots = daily_config.get("slots", {}) or {}
    pool = [paper for paper in candidates if paper.dedupe_key not in excluded_keys]
    picked: List[Tuple[str, Paper, List[str]]] = []
    used_keys: Set[str] = set()
    used_venues: Set[str] = set()
    for slot in SLOT_ORDER:
        terms = list((slots.get(slot) or {}).get("terms", []))
        ranked: List[Tuple[float, Paper, List[str]]] = []
        for paper in pool:
            if paper.dedupe_key in used_keys:
                continue
            score, hits = slot_score(paper, slot, terms, settings, weights)
            if score > 0:
                ranked.append((score, paper, hits))
        ranked.sort(key=lambda item: item[0], reverse=True)
        choice = next(
            (entry for entry in ranked if normalize_text(entry[1].venue) not in used_venues),
            ranked[0] if ranked else None,
        )
        if not choice:
            continue
        _, paper, hits = choice
        picked.append((slot, paper, hits))
        used_keys.add(paper.dedupe_key)
        if paper.venue:
            used_venues.add(normalize_text(paper.venue))
    return picked


def build_record(
    day: str,
    slot: str,
    paper: Paper,
    hits: List[str],
    settings: Dict[str, Any],
    analysis: Optional[Dict[str, Any]],
    backfilled_window: bool,
) -> Dict[str, Any]:
    daily_config = settings.get("daily", {}) or {}
    slot_config = (daily_config.get("slots", {}) or {}).get(slot, {}) or {}
    meta = venue_meta(paper.venue, settings)
    return {
        "date": day,
        "slot": slot,
        "slot_label": str(slot_config.get("label") or SLOT_FALLBACK_LABELS[slot]),
        "dedupe_key": paper.dedupe_key,
        "title": paper.title,
        "authors": paper.authors[:10],
        "venue": paper.venue,
        "venue_meta": {"group": meta.get("group"), "if_2025": meta.get("if_2025"), "rank": meta.get("rank")},
        "publication_date": paper.publication_date or paper.year,
        "doi": paper.doi,
        "doi_url": f"https://doi.org/{paper.doi}" if paper.doi else "",
        "openalex_url": paper.openalex_url,
        "publisher_url": paper.publisher_url,
        "oa_url": paper.oa_url,
        "abstract": paper.abstract,
        "total_score": paper.total_score,
        "slot_hits": hits,
        "backfilled_window": backfilled_window,
        "analysis": analysis,
        "feedback": {"read": False, "starred": False, "useful": None},
    }


def rule_based_reason(record: Dict[str, Any]) -> str:
    pieces = [f"入选槽位：{record['slot_label']}"]
    meta = record.get("venue_meta") or {}
    if meta.get("if_2025"):
        pieces.append(f"期刊 IF {meta['if_2025']}" + (f"（{meta['rank']}）" if meta.get("rank") else ""))
    if record.get("slot_hits"):
        pieces.append("命中信号词：" + "、".join(record["slot_hits"][:5]))
    if record.get("backfilled_window"):
        pieces.append("回溯推荐（近 14 天该槽位无合适新文）")
    return "；".join(pieces) + "。"


def render_daily_report(day: str, records: List[Dict[str, Any]]) -> str:
    lines = [
        f"# 每日精读推荐 {day}",
        "",
        "- 三个槽位：① 当前研究最相关 ② 理论值得学 ③ 方法/��沿",
        "- 合规说明：仅使用公开元数据与摘要，不下载全文。",
        "",
    ]
    for index, record in enumerate(records, start=1):
        meta = record.get("venue_meta") or {}
        badge = record["venue"] or "未知期刊"
        if meta.get("if_2025"):
            badge += f" · IF {meta['if_2025']}"
        if meta.get("rank"):
            badge += f" · {meta['rank']}"
        analysis = record.get("analysis")
        stars = "⭐" * int(analysis["priority"]) if analysis else "⭐⭐⭐"
        lines.extend(
            [
                f"## {index}. 【{record['slot_label']}】{record['title']}",
                "",
                f"- 作者：{'; '.join(record['authors'][:8]) or '未知'}",
                f"- 期刊：{badge}，{record['publication_date'] or '日期未知'}",
                f"- 链接：{record['doi_url'] or record['publisher_url'] or record['openalex_url'] or '暂无'}",
                f"- 精读优先级：{stars}",
                "",
            ]
        )
        if analysis:
            inferred = "（以下解读主要基于标题与期刊推断，请以原文为准）" if analysis.get("inferred") else ""
            lines.extend(
                [
                    f"**为什么值得读**{inferred}：{analysis['why_read']}",
                    "",
                    f"**RQ / 理论**：{analysis['rq_theory']}",
                    "",
                    f"**数据与方法**：{analysis['data_method']}",
                    "",
                    f"**核心发现**：{analysis['findings']}",
                    "",
                    f"**最值得精读**：{analysis['focus_section']}",
                    "",
                    f"**可直接借鉴**：{analysis['takeaways']}",
                    "",
                ]
            )
        else:
            lines.extend([f"**入选理由**：{rule_based_reason(record)}", ""])
        abstract = str(record.get("abstract") or "").strip()
        if abstract:
            if len(abstract) > 900:
                abstract = abstract[:900].rstrip() + "..."
            lines.extend([f"摘要：{abstract}", ""])
        else:
            lines.extend(["摘要：暂无公开摘要（已尝试 OpenAlex、Crossref、Semantic Scholar 三个来源）。", ""])
    return "\n".join(lines).rstrip() + "\n"


def append_feed_records(path: Path, records: List[Dict[str, Any]]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_daily(
    settings: Dict[str, Any],
    base_dir: Path,
    client: Optional[SourceClient] = None,
    llm: Optional[LLMClient] = None,
    day: Optional[str] = None,
    dry_run: bool = False,
    skip_llm: bool = False,
    excluded_keys: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    day = day or today_iso()
    client = client or SourceClient()
    daily_config = settings.get("daily", {}) or {}
    window_days = int(daily_config.get("window_days", 14))
    fallback_months = int(daily_config.get("fallback_window_months", 4))

    feed_path = daily_feed_path(base_dir)
    existing_records = load_feed_records(feed_path)
    if any(record.get("date") == day for record in existing_records):
        return [record for record in existing_records if record.get("date") == day]

    excluded = load_feed_keys(feed_path) | load_recommended_keys(base_dir / "data" / "recommended.csv") | (excluded_keys or set())
    weights = load_feedback_weights(feedback_path(base_dir))

    recent_from = (date.fromisoformat(day) - timedelta(days=window_days)).isoformat()
    candidates = prepare_candidates(collect_candidates(settings, client, recent_from, day), settings)
    picked = select_daily(candidates, settings, excluded, weights)
    recent_slots = {slot for slot, _, _ in picked}
    if len(picked) < len(SLOT_ORDER):
        fallback_from = (date.fromisoformat(day) - timedelta(days=30 * fallback_months)).isoformat()
        fallback_candidates = prepare_candidates(collect_candidates(settings, client, fallback_from, day), settings)
        already = {paper.dedupe_key for _, paper, _ in picked}
        fallback_picks = select_daily(fallback_candidates, settings, excluded | already, weights)
        for slot, paper, hits in fallback_picks:
            if slot not in recent_slots:
                picked.append((slot, paper, hits))
        picked.sort(key=lambda entry: SLOT_ORDER.index(entry[0]))

    papers = [paper for _, paper, _ in picked]
    client.backfill_abstracts(papers)

    llm = llm or LLMClient()
    records = []
    for slot, paper, hits in picked:
        slot_label = str(((daily_config.get("slots", {}) or {}).get(slot) or {}).get("label") or SLOT_FALLBACK_LABELS[slot])
        analysis = None if (skip_llm or not llm.configured) else llm.deep_read(paper, slot_label)
        records.append(build_record(day, slot, paper, hits, settings, analysis, backfilled_window=slot not in recent_slots))

    if not dry_run and records:
        append_feed_records(feed_path, records)
        report_path = daily_report_path(base_dir, day)
        ensure_parent(report_path)
        report_path.write_text(render_daily_report(day, records), encoding="utf-8")
    return records
