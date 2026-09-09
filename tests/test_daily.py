import json
from pathlib import Path

from comm_paper_radar.daily import (
    build_record,
    feedback_adjustment,
    load_feed_keys,
    render_daily_report,
    rule_based_reason,
    select_daily,
)
from comm_paper_radar.llm import parse_analysis
from comm_paper_radar.models import Paper
from comm_paper_radar.scoring import venue_meta


SETTINGS = {
    "authority_sources": {
        "tier_1": {
            "score": 100,
            "names": [
                {"name": "Journal of Computer-Mediated Communication", "if_2025": 7.4, "rank": "Communication 4/227"},
                {"name": "Journal of Communication", "if_2025": 5.3, "rank": "Communication 11/227"},
            ],
        },
        "tier_2": {"score": 92, "names": [{"name": "Communication Methods and Measures"}, {"name": "Communication Theory"}]},
        "ai_computation": {"score": 88, "names": ["ICWSM"]},
    },
    "daily": {
        "count": 3,
        "slots": {
            "relevance": {"label": "当前研究最相关", "terms": ["scam", "framing", "victim"]},
            "theory": {"label": "理论值得学", "terms": ["theoretical framework", "conceptual model"]},
            "method": {"label": "方法/前沿", "terms": ["computational", "large language model", "experiment"]},
        },
    },
}


def make_paper(key: str, title: str, venue: str, abstract: str = "", score: float = 60.0) -> Paper:
    return Paper(dedupe_key=key, title=title, venue=venue, abstract=abstract, total_score=score, doi=f"10.1/{key}")


def test_select_daily_assigns_one_paper_per_slot_not_by_impact_factor():
    papers = [
        make_paper("p1", "Scam victim framing in online comments", "New Media & Society", score=50),
        make_paper("p2", "A theoretical framework for mediated trust", "Communication Theory", score=40),
        make_paper("p3", "Large language model coding for content analysis", "Communication Methods and Measures", score=45),
        make_paper("p4", "Generic high-score paper about television", "Journal of Communication", score=99),
    ]
    picked = select_daily(papers, SETTINGS, excluded_keys=set(), weights={})

    slots = {slot: paper.dedupe_key for slot, paper, _ in picked}
    assert slots == {"relevance": "p1", "theory": "p2", "method": "p3"}


def test_select_daily_skips_already_recommended_and_avoids_duplicates():
    papers = [
        make_paper("old", "Scam framing study", "New Media & Society"),
        make_paper("new", "Another scam victim narrative study", "Journal of Communication"),
    ]
    picked = select_daily(papers, SETTINGS, excluded_keys={"old"}, weights={})
    relevance = next((paper for slot, paper, _ in picked if slot == "relevance"), None)
    assert relevance is not None and relevance.dedupe_key == "new"

    keys = [paper.dedupe_key for _, paper, _ in picked]
    assert len(keys) == len(set(keys))


def test_feedback_adjustment_is_light_touch_and_capped():
    weights = {"Journal of Communication": {"useful": 2, "useless": 0}, "Bad Venue": {"useful": 0, "useless": 20}}
    assert feedback_adjustment("Journal of Communication", weights) == 4.0
    assert feedback_adjustment("Bad Venue", weights) == -10.0
    assert feedback_adjustment("Unknown Venue", weights) == 0.0


def test_build_record_includes_jcr_badge_and_feedback_placeholders():
    paper = make_paper("p1", "A study", "Journal of Computer-Mediated Communication")
    record = build_record("2026-09-09", "relevance", paper, ["scam"], SETTINGS, analysis=None, backfilled_window=False)

    assert record["venue_meta"]["if_2025"] == 7.4
    assert record["venue_meta"]["rank"] == "Communication 4/227"
    assert record["feedback"] == {"read": False, "starred": False, "useful": None}
    assert record["doi_url"] == "https://doi.org/10.1/p1"
    assert "入选槽位" in rule_based_reason(record)


def test_daily_report_renders_three_slots_and_llm_sections():
    paper = make_paper("p1", "Scam study", "Journal of Communication", abstract="An abstract.")
    analysis = {
        "why_read": "紧扣诈骗受害叙事。",
        "rq_theory": "framing 理论。",
        "data_method": "内容分析。",
        "findings": "评论框架影响归因。",
        "focus_section": "方法部分。",
        "takeaways": "可借鉴编码方案。",
        "priority": 5,
        "inferred": False,
    }
    record = build_record("2026-09-09", "relevance", paper, ["scam"], SETTINGS, analysis=analysis, backfilled_window=False)
    report = render_daily_report("2026-09-09", [record])

    assert "【当前研究最相关】" in report
    assert "⭐⭐⭐⭐⭐" in report
    assert "可直接借鉴" in report
    assert "IF 5.3" in report


def test_load_feed_keys_reads_jsonl(tmp_path: Path):
    feed = tmp_path / "daily_feed.jsonl"
    feed.write_text(
        json.dumps({"date": "2026-09-08", "dedupe_key": "doi:10.1/a"}) + "\n" + json.dumps({"date": "2026-09-09", "dedupe_key": "doi:10.1/b"}) + "\n",
        encoding="utf-8",
    )
    assert load_feed_keys(feed) == {"doi:10.1/a", "doi:10.1/b"}


def test_parse_analysis_tolerates_markdown_fences_and_clamps_priority():
    content = """```json
{"why_read": "值得读", "rq_theory": "RQ", "data_method": "方法", "findings": "发现", "focus_section": "部分", "takeaways": "借鉴", "priority": 9}
```"""
    analysis = parse_analysis(content, has_abstract=True)
    assert analysis is not None
    assert analysis["priority"] == 5
    assert analysis["inferred"] is False
    assert parse_analysis("not json at all", has_abstract=True) is None


def test_venue_meta_supports_string_and_dict_entries():
    assert venue_meta("ICWSM", SETTINGS)["group"] == "ai_computation"
    meta = venue_meta("Journal of Computer-Mediated Communication", SETTINGS)
    assert meta["if_2025"] == 7.4 and meta["group"] == "tier_1"
    assert venue_meta("Unknown Journal", SETTINGS) == {}
