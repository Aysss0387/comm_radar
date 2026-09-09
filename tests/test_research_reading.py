from comm_paper_radar.research_reading import card_document, evidence_ids, normalize_draft
from comm_paper_radar.research_workspace import load_card


def test_reading_card_keeps_evidence_snapshot_and_required_sections(tmp_path):
    paper = {"citekey": "Smith2025", "title": "A paper", "collection": "framing", "item_key": "ABCD1234"}
    snapshot = {"path": "research/cache/fulltext/ABCD.json", "attachment_key": "PDF123", "chunks": [{"id": "E001"}]}
    document = card_document(paper, snapshot, "## 阅读概览\n\n结论。【E001】")
    path = tmp_path / "Smith2025.md"
    path.write_text(document, encoding="utf-8")
    metadata, body = load_card(path)
    assert metadata["evidence_file"] == snapshot["path"]
    assert metadata["zotero_item_key"] == "ABCD1234"
    assert "## 原文证据" in body
    assert "## My Thoughts" in body
    assert evidence_ids(body) == ["E001"]


def test_normalize_draft_supplies_missing_sections():
    draft = normalize_draft("## 阅读概览\n\n简述。", {"title": "A paper"})
    assert draft.startswith("# A paper")
    assert "## 主要发现" in draft
    assert "## My Thoughts" in draft
