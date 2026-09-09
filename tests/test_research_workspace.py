from pathlib import Path
import json

import pytest

from comm_paper_radar.research_workspace import (
    ResearchWorkspaceError,
    ZoteroWebClient,
    build_atlas,
    generate_citekey_preview,
    migrate_citekeys,
    preserve_my_thoughts,
    render_card_template,
    render_zotero_note,
    sync_zotero_notes,
    validate_card,
    zotero_web_client_from_env,
)


def write_card(path: Path, citekey: str, title: str, collection: str, theory_name: str, method_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
citekey: {citekey}
title: {title}
collection: {collection}
theories:
  - name: {theory_name}
    definition: A definition
    dimensions: Dimension A
    operationalization: Measure A
    context: Platform X
    outcomes: Outcome Y
    evidence: "[{citekey} ¶12]"
methods:
  - name: {method_name}
    design: Survey
    data_and_sample: 500 respondents
    analysis: Regression
    validation: Robustness check
    research_question: Explain outcome Y
    evidence: "[{citekey} ¶30]"
---

# {title}

## Key Evidence

- [{citekey} ¶12]

## My Thoughts

Personal note.
""",
        encoding="utf-8",
    )


def test_card_template_preserves_manual_notes_and_validates(tmp_path: Path):
    original = render_card_template("Smith2024", "A paper", "Framing") + "My annotation.\n"
    refreshed = preserve_my_thoughts(
        original,
        render_card_template("Smith2024", "A revised title", "Framing"),
    )

    assert "My annotation." in refreshed
    card = tmp_path / "Smith2024.md"
    card.write_text(refreshed, encoding="utf-8")
    assert validate_card(card) == []


def test_build_atlas_groups_cards_and_keeps_multiple_methods(tmp_path: Path):
    cards_dir = tmp_path / "research" / "cards"
    write_card(cards_dir / "Alpha2024.md", "Alpha2024", "Alpha", "Scam", "Framing theory", "Experiment")
    write_card(cards_dir / "Beta2025.md", "Beta2025", "Beta", "Scam", "Framing theory", "Survey")
    write_card(cards_dir / "Other2025.md", "Other2025", "Other", "Other", "Other theory", "Interview")

    theory_path, method_path, card_count = build_atlas(
        cards_dir, "Scam", tmp_path / "research" / "collections" / "scam"
    )

    theory = theory_path.read_text(encoding="utf-8")
    methods = method_path.read_text(encoding="utf-8")
    assert card_count == 2
    assert theory.count("[Alpha2024]") == 1
    assert theory.count("[Beta2025]") == 1
    assert "Other2025" not in theory
    assert "## Experiment" in methods
    assert "## Survey" in methods


def test_invalid_card_requires_evidence_location(tmp_path: Path):
    card = tmp_path / "bad.md"
    card.write_text(
        """---
citekey: Bad2024
title: Bad paper
collection: Test
theories:
  - name: Theory
    evidence: "[Bad2024]"
methods: []
---

## My Thoughts
""",
        encoding="utf-8",
    )

    assert any("段落定位" in issue for issue in validate_card(card))


class FakeBBT:
    def __init__(self, current, regenerated):
        self.current = current
        self.regenerated = regenerated
        self.regenerated_calls = []

    def citationkeys(self, item_keys):
        return {key: self.current[key] for key in item_keys}

    def regenerate_keys(self, citekeys):
        self.regenerated_calls.append(citekeys)
        return {key: self.regenerated[key] for key in citekeys}


class FakeZotero:
    def __init__(self, items, child_notes=None):
        self.items = items
        self.notes = child_notes or []
        self.created = []
        self.updated = []

    def collection_by_name(self, name):
        return {"key": "COLLECT", "data": {"key": "COLLECT", "name": name}}

    def collection_items(self, key):
        return self.items

    def child_notes(self, parent):
        return self.notes

    def create_note(self, parent, content):
        self.created.append((parent, content))
        return "NEWNOTE", 1

    def update_note(self, key, content, version):
        self.updated.append((key, content, version))
        return (version or 0) + 1


def bound_card(path: Path, item_key: str, citekey: str = "Old2024") -> None:
    write_card(path, citekey, "Bound paper", "Framing", "Theory", "Survey")
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace("collection: Framing\n", f"collection: Framing\nzotero_item_key: {item_key}\n"), encoding="utf-8")


def test_citekey_preview_matches_cards_and_detects_formula_collision(tmp_path: Path):
    cards = tmp_path / "research" / "cards"
    cards.mkdir(parents=True)
    bound_card(cards / "OLD1.md", "ITEM1", "OLD1")
    item = {
        "key": "ITEM1",
        "data": {
            "key": "ITEM1", "title": "A Framing Study", "date": "2024-01-01",
            "creators": [{"lastName": "Smith"}], "tags": [{"tag": "⭐⭐⭐⭐⭐"}],
        },
    }
    preview = generate_citekey_preview("Framing", cards, FakeZotero([item]), FakeBBT({"ITEM1": "OLD1"}, {}))

    assert preview["formula"] == 'auth.lower + "-" + year + "-" + shorttitle(3,3)'
    assert preview["entries"][0]["card_path"].endswith("OLD1.md")
    assert preview["entries"][0]["planned_citekey"] == "smith-2024-AFramingStudy"
    assert preview["entries"][0]["status"] == "ready"


def test_migrate_citekeys_renames_card_preserves_thoughts_and_updates_registry(tmp_path: Path):
    cards = tmp_path / "research" / "cards"
    cards.mkdir(parents=True)
    source = cards / "OLD1.md"
    bound_card(source, "ITEM1", "OLD1")
    source.write_text(source.read_text(encoding="utf-8") + "Keep this thought.\n", encoding="utf-8")
    preview = {"entries": [{"status": "ready", "old_citekey": "OLD1", "zotero_item_key": "ITEM1", "card_path": str(source)}], "conflicts": {}}
    registry = tmp_path / "research" / "zotero-sync" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"schema_version": 1, "notes": {"ITEM1": {"citekey": "OLD1", "card_path": str(source)}}}), encoding="utf-8")

    mapping = migrate_citekeys(preview, cards, FakeBBT({}, {"OLD1": "smith-2024-BoundPaper"}), registry)
    target = cards / "smith-2024-BoundPaper.md"

    assert mapping == {"OLD1": "smith-2024-BoundPaper"}
    assert not source.exists()
    assert "citekey: smith-2024-BoundPaper" in target.read_text(encoding="utf-8")
    assert "Keep this thought." in target.read_text(encoding="utf-8")
    assert json.loads(registry.read_text(encoding="utf-8"))["notes"]["ITEM1"]["citekey"] == "smith-2024-BoundPaper"


def test_zotero_sync_creates_then_is_idempotent_without_touching_manual_note(tmp_path: Path):
    cards = tmp_path / "research" / "cards"
    cards.mkdir(parents=True)
    card = cards / "bound.md"
    bound_card(card, "ITEM1", "smith-2024-BoundPaper")
    zotero = FakeZotero([], [{"key": "MANUAL", "data": {"itemType": "note", "note": "my hand-written note"}}])
    registry = tmp_path / "research" / "zotero-sync" / "registry.json"

    report = sync_zotero_notes(cards, "Framing", registry, zotero, dry_run=False)
    assert report[0]["status"] == "created"
    assert len(zotero.created) == 1
    assert "zotero://select/library/items/ITEM1" in zotero.created[0][1]
    assert "my hand-written note" not in zotero.created[0][1]
    assert sync_zotero_notes(cards, "Framing", registry, zotero, dry_run=False)[0]["status"] == "unchanged"
    assert len(zotero.created) == 1


def test_zotero_sync_updates_existing_managed_note_not_manual_note(tmp_path: Path):
    cards = tmp_path / "research" / "cards"
    cards.mkdir(parents=True)
    card = cards / "bound.md"
    bound_card(card, "ITEM1")
    registry = tmp_path / "registry.json"
    zotero = FakeZotero([], [{"key": "MANUAL", "data": {"itemType": "note", "note": "manual"}}, {"key": "MANAGED", "data": {"itemType": "note", "version": 3, "note": "<!-- codex-research-card:v1 parent=ITEM1 -->"}}])

    report = sync_zotero_notes(cards, "Framing", registry, zotero, dry_run=False)
    assert report[0]["status"] == "updated"
    assert zotero.updated[0][0] == "MANAGED"
    assert not zotero.created


def test_failed_note_update_does_not_write_registry(tmp_path: Path):
    cards = tmp_path / "research" / "cards"
    cards.mkdir(parents=True)
    card = cards / "bound.md"
    bound_card(card, "ITEM1")
    registry = tmp_path / "registry.json"
    original = {"schema_version": 1, "notes": {"ITEM1": {"note_key": "MANAGED", "note_version": 3, "content_sha256": "stale"}}}
    registry.write_text(json.dumps(original), encoding="utf-8")

    class FailingZotero(FakeZotero):
        def update_note(self, key, content, version):
            raise ResearchWorkspaceError("concurrent edit")

    with pytest.raises(ResearchWorkspaceError, match="concurrent edit"):
        sync_zotero_notes(cards, "Framing", registry, FailingZotero([]), dry_run=False)
    assert json.loads(registry.read_text(encoding="utf-8")) == original


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.ok = status_code < 400
        self.text = json.dumps(payload) if payload is not None else ""
        self.reason = "reason"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def test_zotero_web_client_requires_credentials(monkeypatch):
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    with pytest.raises(ResearchWorkspaceError, match="ZOTERO_USER_ID"):
        zotero_web_client_from_env()


def test_zotero_web_client_creates_note_with_auth_headers():
    session = FakeSession([FakeResponse(200, {"successful": {"0": {"key": "NOTE1", "version": 7}}})])
    client = ZoteroWebClient("12345", "secret-key", session=session)

    note_key, version = client.create_note("ITEM1", "<p>card</p>")

    assert (note_key, version) == ("NOTE1", 7)
    sent = session.requests[0]
    assert sent["url"] == "https://api.zotero.org/users/12345/items"
    assert sent["headers"]["Zotero-API-Key"] == "secret-key"
    assert sent["json"][0]["parentItem"] == "ITEM1"


def test_zotero_web_client_update_sends_version_and_reads_new_version():
    session = FakeSession([FakeResponse(204, None, {"Last-Modified-Version": "9"})])
    client = ZoteroWebClient("12345", "secret-key", session=session)

    assert client.update_note("NOTE1", "<p>v2</p>", 8) == 9
    assert session.requests[0]["headers"]["If-Unmodified-Since-Version"] == "8"


def test_zotero_web_client_conflict_and_permission_errors_are_clear():
    client = ZoteroWebClient("12345", "secret-key", session=FakeSession([FakeResponse(412)]))
    with pytest.raises(ResearchWorkspaceError, match="412"):
        client.update_note("NOTE1", "<p>v2</p>", 1)
    client = ZoteroWebClient("12345", "secret-key", session=FakeSession([FakeResponse(403)]))
    with pytest.raises(ResearchWorkspaceError, match="写权限"):
        client.create_note("ITEM1", "<p>card</p>")


def test_zotero_note_renderer_requires_parent_key(tmp_path: Path):
    card = tmp_path / "card.md"
    card.write_text(render_card_template("Smith2024", "A paper", "Framing"), encoding="utf-8")
    from comm_paper_radar.research_workspace import load_card
    metadata, body = load_card(card)
    with pytest.raises(ResearchWorkspaceError, match="zotero_item_key"):
        render_zotero_note(card, metadata, body)
