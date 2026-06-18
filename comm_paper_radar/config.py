from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml


DEFAULT_CONFIG_PATH = Path("config/settings.yml")


def load_settings(path: str = None) -> Dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def profile_terms(settings: Dict[str, Any], profile: str = None) -> List[str]:
    profiles = settings.get("profiles", {})
    if profile:
        return list(profiles.get(profile, {}).get("terms", []))
    terms: List[str] = []
    for value in profiles.values():
        terms.extend(value.get("terms", []))
    return unique(terms)


def boosted_profiles(settings: Dict[str, Any]) -> List[str]:
    return [
        key
        for key, value in settings.get("profiles", {}).items()
        if value.get("boost")
    ]


def all_terms(settings: Dict[str, Any]) -> List[str]:
    return profile_terms(settings)


def unique(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output

