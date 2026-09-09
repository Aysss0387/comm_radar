import csv
from pathlib import Path
from typing import Dict, Iterable, List, Set

from .models import CSV_FIELDS, RECOMMENDED_FIELDS, Paper
from .utils import today_iso


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_papers(path: Path) -> Dict[str, Paper]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {row.get("dedupe_key", ""): Paper.from_csv_row(row) for row in reader if row.get("dedupe_key")}


def save_papers(path: Path, papers_by_key: Dict[str, Paper]) -> None:
    ensure_parent(path)
    rows = sorted(papers_by_key.values(), key=lambda paper: (paper.total_score, paper.publication_date), reverse=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for paper in rows:
            writer.writerow(paper.to_csv_row())


def merge_papers(existing: Dict[str, Paper], incoming: Iterable[Paper]) -> Dict[str, Paper]:
    now = today_iso()
    merged = dict(existing)
    for paper in incoming:
        if not paper.dedupe_key:
            continue
        current = merged.get(paper.dedupe_key)
        if current:
            current.citation_count = max(current.citation_count, paper.citation_count)
            current.last_seen = now
            current.publisher_url = current.publisher_url or paper.publisher_url
            current.oa_url = current.oa_url or paper.oa_url
            current.openalex_url = current.openalex_url or paper.openalex_url
            current.abstract = current.abstract or paper.abstract
            current.keywords_hit = paper.keywords_hit or current.keywords_hit
            current.authority_score = paper.authority_score
            current.keyword_score = paper.keyword_score
            current.citation_score = paper.citation_score
            current.recency_score = paper.recency_score
            current.total_score = paper.total_score
            current.region = paper.region or current.region
            current.venue = current.venue or paper.venue
        else:
            paper.first_seen = now
            paper.last_seen = now
            merged[paper.dedupe_key] = paper
    return merged


def load_recommended_keys(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {row.get("dedupe_key", "") for row in reader if row.get("dedupe_key")}


def append_recommended(path: Path, papers: List[Paper], week: str, report_path: Path) -> None:
    ensure_parent(path)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECOMMENDED_FIELDS)
        if not exists:
            writer.writeheader()
        for rank, paper in enumerate(papers, start=1):
            writer.writerow(
                {
                    "dedupe_key": paper.dedupe_key,
                    "recommended_week": week,
                    "recommended_rank": rank,
                    "title": paper.title,
                    "region": paper.region,
                    "total_score": f"{paper.total_score:.2f}",
                    "report_path": str(report_path),
                }
            )
            paper.recommended_week = week
            paper.recommended_rank = str(rank)

