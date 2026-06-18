import os
import re
from html import unescape
from typing import Dict, Iterable, List, Optional

import requests

from .models import Paper
from .utils import abstract_from_openalex, env_headers, normalize_doi, slug_title


OPENALEX_URL = "https://api.openalex.org/works"
CROSSREF_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"


class SourceClient:
    def __init__(self, contact_email: str = "", openalex_api_key: str = "", semantic_scholar_api_key: str = ""):
        self.contact_email = contact_email or os.getenv("CONTACT_EMAIL", "")
        self.openalex_api_key = openalex_api_key or os.getenv("OPENALEX_API_KEY", "")
        self.semantic_scholar_api_key = semantic_scholar_api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
        self.session = requests.Session()

    def fetch_openalex(
        self,
        terms: Iterable[str],
        from_date: str,
        to_date: str,
        per_page: int = 100,
        max_pages: int = 5,
        journal: str = "",
    ) -> List[Paper]:
        papers: List[Paper] = []
        query_terms = [term for term in terms if term]
        query = journal or " OR ".join(query_terms[:25]) or "communication"
        cursor = "*"

        for _ in range(max_pages):
            filters = [
                f"from_publication_date:{from_date}",
                f"to_publication_date:{to_date}",
                "type:article",
            ]
            if journal:
                filters.append(f"primary_location.source.display_name.search:{journal}")
            params = {
                "filter": ",".join(filters),
                "search": query,
                "per-page": str(per_page),
                "cursor": cursor,
                "mailto": self.contact_email,
                "select": ",".join(
                    [
                        "id",
                        "doi",
                        "title",
                        "display_name",
                        "publication_date",
                        "publication_year",
                        "abstract_inverted_index",
                        "authorships",
                        "primary_location",
                        "open_access",
                        "cited_by_count",
                    ]
                ),
            }
            headers = env_headers(self.contact_email)
            if self.openalex_api_key:
                headers["Authorization"] = f"Bearer {self.openalex_api_key}"
            response = self.session.get(OPENALEX_URL, params=params, headers=headers, timeout=45)
            if response.status_code >= 400:
                break
            payload = response.json()
            results = payload.get("results", [])
            papers.extend(openalex_to_paper(item) for item in results if item.get("title") or item.get("display_name"))
            next_cursor = payload.get("meta", {}).get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return papers

    def fetch_crossref(
        self,
        terms: Iterable[str],
        from_date: str,
        to_date: str,
        rows: int = 50,
        journal: str = "",
    ) -> List[Paper]:
        query = journal or " OR ".join([term for term in terms if term][:10]) or "communication"
        params = {
            "query.bibliographic": query,
            "filter": f"from-pub-date:{from_date},until-pub-date:{to_date},type:journal-article",
            "rows": str(rows),
            "mailto": self.contact_email,
        }
        response = self.session.get(CROSSREF_URL, params=params, headers=env_headers(self.contact_email), timeout=45)
        if response.status_code >= 400:
            return []
        items = response.json().get("message", {}).get("items", [])
        return [crossref_to_paper(item) for item in items if item.get("title")]

    def enrich_semantic_scholar(self, papers: List[Paper], batch_size: int = 100) -> List[Paper]:
        ids = []
        paper_by_id: Dict[str, Paper] = {}
        for paper in papers:
            external_id = None
            if paper.doi:
                external_id = f"DOI:{paper.doi}"
            elif paper.openalex_id:
                external_id = paper.openalex_id
            if external_id:
                ids.append(external_id)
                paper_by_id[external_id] = paper
        if not ids:
            return papers

        headers = env_headers(self.contact_email)
        if self.semantic_scholar_api_key:
            headers["x-api-key"] = self.semantic_scholar_api_key
        fields = "title,citationCount,url,openAccessPdf,externalIds,publicationDate,venue"
        for start in range(0, len(ids), batch_size):
            chunk = ids[start : start + batch_size]
            response = self.session.post(
                SEMANTIC_SCHOLAR_BATCH_URL,
                params={"fields": fields},
                json={"ids": chunk},
                headers=headers,
                timeout=45,
            )
            if response.status_code >= 400:
                continue
            for requested_id, item in zip(chunk, response.json()):
                if not item:
                    continue
                paper = paper_by_id.get(requested_id)
                if not paper:
                    continue
                paper.citation_count = max(paper.citation_count, int(item.get("citationCount") or 0))
                if not paper.publisher_url:
                    paper.publisher_url = item.get("url") or ""
                oa = item.get("openAccessPdf") or {}
                if not paper.oa_url:
                    paper.oa_url = oa.get("url") or ""
                if not paper.venue:
                    paper.venue = item.get("venue") or ""
        return papers


def openalex_to_paper(item: Dict) -> Paper:
    doi = normalize_doi(item.get("doi") or "")
    openalex_id = item.get("id") or ""
    openalex_url = openalex_id
    title = clean_text(item.get("title") or item.get("display_name") or "")
    year = str(item.get("publication_year") or "")
    publication_date = item.get("publication_date") or ""
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    open_access = item.get("open_access") or {}
    authors = []
    countries = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(author["display_name"])
        for institution in authorship.get("institutions") or []:
            country = institution.get("country_code")
            if country:
                countries.append(country)
    dedupe_key = make_dedupe_key(doi, openalex_id, title, year)
    return Paper(
        dedupe_key=dedupe_key,
        title=title,
        authors=authors,
        year=year,
        publication_date=publication_date,
        venue=source.get("display_name") or "",
        doi=doi,
        openalex_id=openalex_id,
        openalex_url=openalex_url,
        publisher_url=primary.get("landing_page_url") or "",
        oa_url=open_access.get("oa_url") or "",
        abstract=abstract_from_openalex(item.get("abstract_inverted_index") or {}),
        citation_count=int(item.get("cited_by_count") or 0),
        institution_countries=sorted(set(countries)),
    )


def crossref_to_paper(item: Dict) -> Paper:
    doi = normalize_doi(item.get("DOI") or "")
    title = clean_text(" ".join(item.get("title") or []))
    year = ""
    date_parts = (item.get("published-print") or item.get("published-online") or item.get("published") or {}).get("date-parts") or []
    publication_date = ""
    if date_parts:
        parts = date_parts[0]
        year = str(parts[0]) if parts else ""
        month = str(parts[1] if len(parts) > 1 else 1).zfill(2)
        day = str(parts[2] if len(parts) > 2 else 1).zfill(2)
        publication_date = f"{year}-{month}-{day}"
    venue = " ".join(item.get("container-title") or [])
    authors = []
    for author in item.get("author") or []:
        name = " ".join([author.get("given", ""), author.get("family", "")]).strip()
        if name:
            authors.append(name)
    abstract = item.get("abstract") or ""
    return Paper(
        dedupe_key=make_dedupe_key(doi, "", title, year),
        title=title,
        authors=authors,
        year=year,
        publication_date=publication_date,
        venue=venue,
        doi=doi,
        publisher_url=item.get("URL") or "",
        abstract=strip_crossref_abstract(abstract),
        citation_count=int(item.get("is-referenced-by-count") or 0),
    )


def make_dedupe_key(doi: str, openalex_id: str, title: str, year: str) -> str:
    if doi:
        return f"doi:{normalize_doi(doi)}"
    if openalex_id:
        return f"openalex:{openalex_id.rsplit('/', 1)[-1]}"
    return f"title:{slug_title(title)}:{year}"


def strip_crossref_abstract(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"<[^>]+>", " ", value).strip()


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()
