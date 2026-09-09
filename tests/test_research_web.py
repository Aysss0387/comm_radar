import json
import shutil
from pathlib import Path

from comm_paper_radar.research_web import AIService, create_app


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


def test_deepseek_console_url_is_normalized_to_api_endpoint(tmp_path: Path):
    service = AIService(tmp_path)
    settings = service.configure({"base_url": "https://platform.deepseek.com", "model": "deepseek-v4-pro"})
    assert settings["base_url"] == "https://api.deepseek.com"
    assert settings["notice"]
    assert service._chat_url() == "https://api.deepseek.com/chat/completions"
