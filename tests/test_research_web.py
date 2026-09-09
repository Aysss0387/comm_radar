import json
import shutil
from pathlib import Path

from comm_paper_radar.research_web import AIService, DailyStore, create_app


def make_web_project(tmp_path: Path) -> Path:
    shutil.copytree(Path(__file__).parents[1] / "web", tmp_path / "web")
    cards = tmp_path / "research" / "cards"
    cards.mkdir(parents=True)
    (cards / "6FAIDITS.md").write_text(
        """---
citekey: 6FAIDITS
title: First study
collection: framing
theories:
  - name: Framing theory
    evidence: "[6FAIDITS ¶2]"
methods:
  - name: Content analysis
    evidence: "[6FAIDITS ¶3]"
---

# First study

## Research Questions / Hypotheses

First question.

## My Thoughts

""",
        encoding="utf-8",
    )
    second = cards / "Second.md"
    second.write_text(
        """---
citekey: Second
title: Second study
collection: framing
theories:
  - name: Framing analysis
    evidence: "[Second ¶2]"
methods:
  - name: Content analysis
    evidence: "[Second ¶3]"
---

# Second study

## Research Questions / Hypotheses

Second question.

## My Thoughts

""",
        encoding="utf-8",
    )
    return tmp_path


def client_with_token(tmp_path: Path):
    app = create_app(make_web_project(tmp_path))
    app.testing = True
    client = app.test_client()
    token = client.get("/api/session").get_json()["token"]
    return client, {"X-Research-Session": token}


def test_web_serves_ui_and_protects_api(tmp_path: Path):
    client, headers = client_with_token(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/api/collections").status_code == 403
    response = client.get("/api/collections", headers=headers)
    assert "framing" in response.get_json()["collections"]


def test_web_saves_personal_fields_with_version_guard(tmp_path: Path):
    client, headers = client_with_token(tmp_path)
    detail = client.get("/api/cards/6FAIDITS?collection=framing", headers=headers).get_json()
    response = client.patch(
        "/api/cards/6FAIDITS/personal?collection=framing",
        headers=headers,
        data=json.dumps({"base_hash": detail["summary"]["hash"], "my_thoughts": "A question to revisit.", "reading_status": "精读中", "questions": [{"text": "What is compared?", "done": False}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.get_json()["summary"]["reading_status"] == "精读中"
    stale = client.patch(
        "/api/cards/6FAIDITS/personal?collection=framing",
        headers=headers,
        data=json.dumps({"base_hash": detail["summary"]["hash"], "my_thoughts": "stale"}),
        content_type="application/json",
    )
    assert stale.status_code == 409


def test_web_compare_and_map_return_traceable_data(tmp_path: Path):
    client, headers = client_with_token(tmp_path)
    compare = client.post("/api/compare", headers=headers, json={"collection": "framing", "citekeys": ["6FAIDITS", "Second"]})
    assert compare.status_code == 200
    assert [row["label"] for row in compare.get_json()["rows"]][:2] == ["研究问题", "理论与概念"]
    atlas = client.get("/api/map?collection=framing", headers=headers)
    assert atlas.status_code == 200
    data = atlas.get_json()
    assert data["card_count"] == 2
    assert any(link["citekey"] == "Second" for link in data["links"])


def test_deepseek_console_url_is_normalized_to_api_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    service = AIService(tmp_path)
    settings = service.configure({"base_url": "https://platform.deepseek.com", "model": "deepseek-v4-pro"})
    assert settings["base_url"] == "https://api.deepseek.com"
    assert settings["notice"]
    assert service._chat_url() == "https://api.deepseek.com/chat/completions"


def test_deepseek_env_configures_ai_and_never_returns_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-server-secret")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    service = AIService(tmp_path)
    settings = service.public_settings()
    assert settings["configured"] is True
    assert settings["managed"] is True
    assert settings["base_url"] == "https://api.deepseek.com"
    assert settings["model"] == "deepseek-chat"
    assert "sk-server-secret" not in json.dumps(settings)
    result = service.configure({"api_key": "sk-from-browser", "model": "other-model"})
    assert service.api_key == "sk-server-secret"
    assert service.model == "deepseek-chat"
    assert result["notice"]
    assert "sk" not in json.dumps(result).replace("sk-", "") or "sk-server-secret" not in json.dumps(result)


def test_published_env_rejects_browser_submitted_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    service = AIService(tmp_path)
    result = service.configure({"api_key": "sk-from-browser", "model": "deepseek-chat", "base_url": "https://api.deepseek.com"})
    assert service.api_key == ""
    assert result["configured"] is False
    assert "DEEPSEEK_API_KEY" in result["notice"]
    test_result = service.test()
    assert test_result["ok"] is False
    assert "DEEPSEEK_API_KEY" in test_result["message"]


def test_daily_store_reuses_persisted_day_without_regenerating(tmp_path: Path, monkeypatch):
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    papers = [{"date": today, "slot": slot, "dedupe_key": slot} for slot in ("relevance", "theory", "method")]

    class FakeDatabase:
        available = True

        def day(self, day=None):
            return {"date": day or today, "dates": [today], "papers": papers}

        def history(self):
            return papers

    monkeypatch.setattr("comm_paper_radar.research_web.run_daily", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应重新生成")))
    result = DailyStore(tmp_path, FakeDatabase()).generate()
    assert result["generated"] is False
    assert len(result["papers"]) == 3


def test_daily_store_saves_new_generation_to_database(tmp_path: Path, monkeypatch):
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    generated = [{"date": today, "slot": slot, "dedupe_key": slot} for slot in ("relevance", "theory", "method")]

    class FakeDatabase:
        available = True

        def __init__(self):
            self.records = []

        def day(self, day=None):
            return {"date": day or today, "dates": [today] if self.records else [], "papers": self.records}

        def excluded_paper_ids(self):
            return {"old"}

        def save_recommendations(self, day, records):
            self.records = list(records)
            return self.records

    database = FakeDatabase()
    monkeypatch.setattr("comm_paper_radar.research_web.load_settings", lambda path: {})
    monkeypatch.setattr("comm_paper_radar.research_web.run_daily", lambda *args, **kwargs: generated)
    result = DailyStore(tmp_path, database).generate()
    assert result["generated"] is True
    assert database.records == generated


def test_published_settings_use_zotero_web_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ZOTERO_USER_ID", "12345")
    monkeypatch.setenv("ZOTERO_API_KEY", "zotero-secret")

    class FakeWebClient:
        def collections(self):
            return [{"data": {"name": "framing"}}, {"data": {"name": "scam"}}]

        def can_write(self):
            return True

    import comm_paper_radar.research_web as research_web_module

    monkeypatch.setattr(research_web_module, "zotero_web_client_from_env", lambda session=None: FakeWebClient())
    client, headers = client_with_token(tmp_path)
    payload = client.get("/api/settings", headers=headers).get_json()
    assert payload["zotero"]["mode"] == "web"
    assert payload["zotero"]["connected"] is True
    assert payload["zotero"]["collection_count"] == 2
    assert payload["zotero"]["writable"] is True
    assert "zotero-secret" not in json.dumps(payload)
    assert "12345" not in json.dumps(payload["zotero"])
