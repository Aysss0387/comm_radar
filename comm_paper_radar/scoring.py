import math
from datetime import date
from typing import Any, Dict, Iterable, List, Tuple

from .models import Paper
from .utils import has_chinese, normalize_text, parse_date


def classify_region(paper: Paper, settings: Dict[str, Any]) -> str:
    domestic_names = []
    for group_name, group in (settings.get("authority_sources") or {}).items():
        if group_name == "domestic":
            domestic_names.extend(group.get("names", []))
    venue_text = normalize_text(paper.venue)
    if "CN" in set(paper.institution_countries):
        return "domestic"
    if any(normalize_text(name) in venue_text for name in domestic_names):
        return "domestic"
    if has_chinese(f"{paper.title} {paper.abstract}"):
        return "domestic"
    return "international"


def score_papers(papers: List[Paper], settings: Dict[str, Any], today: date = None) -> List[Paper]:
    today = today or date.today()
    max_citations = max([paper.citation_count for paper in papers] + [1])
    for paper in papers:
        labels, hits, keyword_score = keyword_score_for(paper, settings)
        paper.topic_labels = labels
        paper.keywords_hit = hits
        paper.region = classify_region(paper, settings)
        paper.authority_score = authority_score_for(paper, settings)
        paper.keyword_score = keyword_score
        paper.citation_score = citation_score_for(paper.citation_count, max_citations)
        paper.recency_score = recency_score_for(paper.publication_date, today)
        paper.total_score = round(
            paper.authority_score * 0.35
            + paper.keyword_score * 0.30
            + paper.citation_score * 0.20
            + paper.recency_score * 0.15,
            2,
        )
    return papers


def keyword_score_for(paper: Paper, settings: Dict[str, Any]) -> Tuple[List[str], List[str], float]:
    haystack_title = normalize_text(paper.title)
    haystack_abstract = normalize_text(paper.abstract)
    haystack_venue = normalize_text(paper.venue)
    labels: List[str] = []
    hits: List[str] = []
    raw = 0.0
    profiles = settings.get("profiles") or {}

    for profile_id, profile in profiles.items():
        profile_hits = []
        for term in profile.get("terms", []):
            term_norm = normalize_text(term)
            if not term_norm:
                continue
            if term_norm in haystack_title:
                raw += 7
                profile_hits.append(term)
            elif term_norm in haystack_abstract:
                raw += 4
                profile_hits.append(term)
            elif term_norm in haystack_venue:
                raw += 2
                profile_hits.append(term)
        if profile_hits:
            labels.append(profile.get("label") or profile_id)
            hits.extend(profile_hits[:6])
            if profile.get("boost"):
                raw += 8

    for term in settings.get("exclude_terms") or []:
        term_norm = normalize_text(term)
        if term_norm and term_norm in f"{haystack_title} {haystack_abstract}":
            raw -= 35

    return unique(labels), unique(hits), max(0.0, min(100.0, raw))


def authority_score_for(paper: Paper, settings: Dict[str, Any]) -> float:
    venue = normalize_text(paper.venue)
    if not venue:
        return 35.0
    best = 45.0
    for group in (settings.get("authority_sources") or {}).values():
        group_score = float(group.get("score", 70))
        for name in group.get("names", []):
            name_norm = normalize_text(name)
            if name_norm and (name_norm == venue or name_norm in venue or venue in name_norm):
                best = max(best, group_score)
    return min(100.0, best)


def citation_score_for(citation_count: int, max_citations: int) -> float:
    if citation_count <= 0 or max_citations <= 0:
        return 0.0
    return round(100.0 * (math.log1p(citation_count) / math.log1p(max_citations)), 2)


def recency_score_for(publication_date: str, today: date = None) -> float:
    today = today or date.today()
    parsed = parse_date(publication_date)
    if not parsed:
        return 30.0
    age_days = max(0, (today - parsed).days)
    if age_days <= 30:
        return 100.0
    if age_days >= 122:
        return 20.0
    return round(100.0 - ((age_days - 30) / 92.0) * 80.0, 2)


def should_keep(paper: Paper) -> bool:
    return bool(paper.title) and paper.keyword_score >= 8 and paper.total_score > 0


def passes_quality_filters(paper: Paper, settings: Dict[str, Any]) -> bool:
    title = normalize_text(paper.title)
    if not title:
        return False
    for pattern in settings.get("exclude_title_patterns") or []:
        if normalize_text(pattern) in title:
            return False
    return True


def select_weekly(papers: List[Paper], recommended_keys: Iterable[str], domestic_count: int, international_count: int) -> List[Paper]:
    seen = set(recommended_keys)
    fresh = [paper for paper in papers if paper.dedupe_key not in seen]
    domestic = sorted([paper for paper in fresh if paper.region == "domestic"], key=lambda item: item.total_score, reverse=True)
    international = sorted([paper for paper in fresh if paper.region != "domestic"], key=lambda item: item.total_score, reverse=True)
    selected = domestic[:domestic_count] + international[:international_count]
    if len(selected) < domestic_count + international_count:
        selected_keys = {paper.dedupe_key for paper in selected}
        fallback = sorted(
            [paper for paper in fresh if paper.dedupe_key not in selected_keys],
            key=lambda item: item.total_score,
            reverse=True,
        )
        for paper in fallback:
            if len(selected) >= domestic_count + international_count:
                break
            selected.append(paper)
    return selected


def unique(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        key = normalize_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output
