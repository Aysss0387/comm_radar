from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import profile_terms
from .models import Paper
from .report import default_weekly_report_path, write_report
from .scoring import passes_quality_filters, score_papers, select_weekly, should_keep
from .sources import SourceClient
from .storage import append_recommended, load_papers, load_recommended_keys, merge_papers, save_papers
from .utils import months_ago, today_iso, unique_by_key, week_id


def run_weekly(
    settings: Dict[str, Any],
    base_dir: Path,
    client: Optional[SourceClient] = None,
    from_date: str = None,
    to_date: str = None,
    dry_run: bool = False,
) -> List[Paper]:
    from_date = from_date or months_ago(30, int(settings.get("window_months", 4)))
    to_date = to_date or today_iso()
    client = client or SourceClient()
    candidates = collect_candidates(settings, client, from_date, to_date)
    scored = prepare_candidates(candidates, settings)
    papers_path = base_dir / "data" / "papers.csv"
    recommended_path = base_dir / "data" / "recommended.csv"
    existing = load_papers(papers_path)
    merged = merge_papers(existing, scored)

    selected = select_weekly(
        list(merged.values()),
        load_recommended_keys(recommended_path),
        int(settings.get("weekly_domestic_count", 2)),
        int(settings.get("weekly_international_count", 8)),
    )
    client.backfill_abstracts(selected)
    report_path = default_weekly_report_path(base_dir)

    if not dry_run:
        write_report(report_path, selected, title=f"传播学/计算传播论文周报 {week_id()}", mode="weekly")
        append_recommended(recommended_path, selected, week_id(), report_path)
        for paper in selected:
            if paper.dedupe_key in merged:
                merged[paper.dedupe_key].recommended_week = paper.recommended_week
                merged[paper.dedupe_key].recommended_rank = paper.recommended_rank
        save_papers(papers_path, merged)
    return selected


def run_search(
    settings: Dict[str, Any],
    base_dir: Path,
    client: Optional[SourceClient] = None,
    profile: str = None,
    keywords: str = "",
    journal: str = "",
    region: str = "all",
    from_date: str = None,
    to_date: str = None,
    top: int = 20,
    report_path: Path = None,
    include_seen: bool = False,
    mark_recommended: bool = False,
    dry_run: bool = False,
) -> List[Paper]:
    from_date = from_date or months_ago(30, int(settings.get("window_months", 4)))
    to_date = to_date or today_iso()
    client = client or SourceClient()
    terms = manual_terms(settings, profile, keywords)
    candidates = collect_candidates(settings, client, from_date, to_date, terms=terms, journal=journal)
    scored = prepare_candidates(candidates, settings)
    if region in {"domestic", "international"}:
        scored = [paper for paper in scored if paper.region == region]

    papers_path = base_dir / "data" / "papers.csv"
    recommended_path = base_dir / "data" / "recommended.csv"
    existing = load_papers(papers_path)
    merged = merge_papers(existing, scored)
    recommended_keys = load_recommended_keys(recommended_path)
    pool = list(merged.values()) if include_seen else [paper for paper in merged.values() if paper.dedupe_key not in recommended_keys]
    if region in {"domestic", "international"}:
        pool = [paper for paper in pool if paper.region == region]
    selected = sorted(pool, key=lambda paper: paper.total_score, reverse=True)[:top]
    report_path = report_path or base_dir / "reports" / "manual" / f"manual-{week_id()}-{date.today().strftime('%H%M%S')}.md"

    if not dry_run:
        write_report(report_path, selected, title="传播学/计算传播专题检索报告", mode="manual")
        if mark_recommended:
            append_recommended(recommended_path, selected, week_id(), report_path)
        save_papers(papers_path, merged)
    return selected


def collect_candidates(
    settings: Dict[str, Any],
    client: SourceClient,
    from_date: str,
    to_date: str,
    terms: Iterable[str] = None,
    journal: str = "",
) -> List[Paper]:
    query_terms = list(terms) if terms is not None else profile_terms(settings)
    papers: List[Paper] = []
    papers.extend(
        client.fetch_openalex(
            query_terms,
            from_date,
            to_date,
            per_page=int(settings.get("openalex_per_page", 100)),
            max_pages=int(settings.get("openalex_max_pages", 5)),
            journal=journal,
        )
    )
    papers.extend(
        client.fetch_crossref(
            query_terms,
            from_date,
            to_date,
            rows=int(settings.get("crossref_rows", 50)),
            journal=journal,
        )
    )
    papers = unique_by_key(papers, lambda paper: paper.dedupe_key)
    return client.enrich_semantic_scholar(papers, int(settings.get("semantic_scholar_batch_size", 100)))


def prepare_candidates(candidates: List[Paper], settings: Dict[str, Any]) -> List[Paper]:
    candidates = [paper for paper in candidates if passes_quality_filters(paper, settings)]
    scored = score_papers(candidates, settings)
    return sorted([paper for paper in scored if should_keep(paper)], key=lambda paper: paper.total_score, reverse=True)


def manual_terms(settings: Dict[str, Any], profile: str = None, keywords: str = "") -> List[str]:
    terms = profile_terms(settings, profile)
    if keywords:
        terms.extend([part.strip() for part in keywords.replace("，", ",").split(",") if part.strip()])
        if "," not in keywords and "，" not in keywords:
            terms.append(keywords.strip())
    return [term for term in terms if term]
