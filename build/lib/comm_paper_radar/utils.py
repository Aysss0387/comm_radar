import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def today_iso() -> str:
    return date.today().isoformat()


def week_id(target: Optional[date] = None) -> str:
    target = target or date.today()
    year, week, _ = target.isocalendar()
    return f"{year}-W{week:02d}"


def parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def months_ago(days_per_month: int, months: int) -> str:
    return (date.today() - timedelta(days=days_per_month * months)).isoformat()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_doi(value: str) -> str:
    value = normalize_text(value)
    value = value.replace("https://doi.org/", "").replace("http://doi.org/", "")
    value = value.replace("doi:", "")
    return value.strip()


def slug_title(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    return value.strip("-")[:120]


def has_chinese(value: str) -> bool:
    return bool(CHINESE_RE.search(value or ""))


def unique_by_key(items: Iterable, key_fn):
    seen = set()
    output = []
    for item in items:
        key = key_fn(item)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def env_headers(contact_email: str = "") -> Dict[str, str]:
    headers = {"User-Agent": "comm-paper-radar/0.1"}
    if contact_email:
        headers["User-Agent"] = f"comm-paper-radar/0.1 (mailto:{contact_email})"
    return headers


def abstract_from_openalex(index: Dict[str, List[int]]) -> str:
    if not index:
        return ""
    words = {}
    for word, positions in index.items():
        for position in positions:
            words[position] = word
    return " ".join(words[index] for index in sorted(words))

