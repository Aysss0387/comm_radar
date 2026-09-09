from dataclasses import dataclass, field
from typing import Dict, List, Optional


CSV_FIELDS = [
    "dedupe_key",
    "title",
    "authors",
    "year",
    "publication_date",
    "venue",
    "region",
    "doi",
    "openalex_id",
    "openalex_url",
    "publisher_url",
    "oa_url",
    "abstract",
    "keywords_hit",
    "citation_count",
    "authority_score",
    "keyword_score",
    "citation_score",
    "recency_score",
    "total_score",
    "first_seen",
    "last_seen",
    "recommended_week",
    "recommended_rank",
]

RECOMMENDED_FIELDS = [
    "dedupe_key",
    "recommended_week",
    "recommended_rank",
    "title",
    "region",
    "total_score",
    "report_path",
]


@dataclass
class Paper:
    dedupe_key: str
    title: str
    authors: List[str] = field(default_factory=list)
    year: str = ""
    publication_date: str = ""
    venue: str = ""
    region: str = "international"
    doi: str = ""
    openalex_id: str = ""
    openalex_url: str = ""
    publisher_url: str = ""
    oa_url: str = ""
    abstract: str = ""
    keywords_hit: List[str] = field(default_factory=list)
    citation_count: int = 0
    authority_score: float = 0
    keyword_score: float = 0
    citation_score: float = 0
    recency_score: float = 0
    total_score: float = 0
    first_seen: str = ""
    last_seen: str = ""
    recommended_week: str = ""
    recommended_rank: str = ""
    topic_labels: List[str] = field(default_factory=list)
    institution_countries: List[str] = field(default_factory=list)

    def to_csv_row(self) -> Dict[str, str]:
        return {
            "dedupe_key": self.dedupe_key,
            "title": self.title,
            "authors": "; ".join(self.authors),
            "year": self.year,
            "publication_date": self.publication_date,
            "venue": self.venue,
            "region": self.region,
            "doi": self.doi,
            "openalex_id": self.openalex_id,
            "openalex_url": self.openalex_url,
            "publisher_url": self.publisher_url,
            "oa_url": self.oa_url,
            "abstract": self.abstract,
            "keywords_hit": "; ".join(self.keywords_hit),
            "citation_count": str(int(self.citation_count or 0)),
            "authority_score": _fmt(self.authority_score),
            "keyword_score": _fmt(self.keyword_score),
            "citation_score": _fmt(self.citation_score),
            "recency_score": _fmt(self.recency_score),
            "total_score": _fmt(self.total_score),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "recommended_week": self.recommended_week,
            "recommended_rank": str(self.recommended_rank),
        }

    @classmethod
    def from_csv_row(cls, row: Dict[str, str]) -> "Paper":
        return cls(
            dedupe_key=row.get("dedupe_key", ""),
            title=row.get("title", ""),
            authors=_split(row.get("authors", "")),
            year=row.get("year", ""),
            publication_date=row.get("publication_date", ""),
            venue=row.get("venue", ""),
            region=row.get("region", "international") or "international",
            doi=row.get("doi", ""),
            openalex_id=row.get("openalex_id", ""),
            openalex_url=row.get("openalex_url", ""),
            publisher_url=row.get("publisher_url", ""),
            oa_url=row.get("oa_url", ""),
            abstract=row.get("abstract", ""),
            keywords_hit=_split(row.get("keywords_hit", "")),
            citation_count=_int(row.get("citation_count")),
            authority_score=_float(row.get("authority_score")),
            keyword_score=_float(row.get("keyword_score")),
            citation_score=_float(row.get("citation_score")),
            recency_score=_float(row.get("recency_score")),
            total_score=_float(row.get("total_score")),
            first_seen=row.get("first_seen", ""),
            last_seen=row.get("last_seen", ""),
            recommended_week=row.get("recommended_week", ""),
            recommended_rank=row.get("recommended_rank", ""),
        )


def _split(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def _int(value: Optional[str]) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def _float(value: Optional[str]) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0


def _fmt(value: float) -> str:
    return f"{float(value):.2f}"

