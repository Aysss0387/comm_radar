from datetime import date

from comm_paper_radar.config import load_settings
from comm_paper_radar.models import Paper
from comm_paper_radar.scoring import classify_region, keyword_score_for, passes_quality_filters, score_papers, select_weekly
from comm_paper_radar.sources import make_dedupe_key


def settings():
    return load_settings("config/settings.yml")


def test_dedupe_prefers_doi_then_openalex_then_title():
    assert make_dedupe_key("https://doi.org/10.123/ABC", "W1", "Title", "2026") == "doi:10.123/abc"
    assert make_dedupe_key("", "https://openalex.org/W123", "Title", "2026") == "openalex:W123"
    assert make_dedupe_key("", "", "A Study of AI Communication!", "2026").startswith("title:a-study-of-ai-communication:2026")


def test_region_classification_domestic_signals():
    cfg = settings()
    cn_institution = Paper(dedupe_key="a", title="Platform governance", institution_countries=["CN"])
    chinese_title = Paper(dedupe_key="b", title="人工智能传播研究")
    domestic_venue = Paper(dedupe_key="c", title="news", venue="新闻与传播研究")
    international = Paper(dedupe_key="d", title="AI communication", venue="New Media & Society")

    assert classify_region(cn_institution, cfg) == "domestic"
    assert classify_region(chinese_title, cfg) == "domestic"
    assert classify_region(domestic_venue, cfg) == "domestic"
    assert classify_region(international, cfg) == "international"


def test_profiles_hit_core_fraud_and_ai():
    cfg = settings()
    paper = Paper(
        dedupe_key="x",
        title="Generative AI misinformation warning messages for online fraud prevention",
        abstract="This study examines risk communication on social media and phishing scams.",
    )
    labels, hits, score = keyword_score_for(paper, cfg)

    assert "传播学/计算传播" in labels
    assert "反诈/网络诈骗" in labels
    assert "人工智能与传播" in labels
    assert any("fraud" in hit.lower() for hit in hits)
    assert score > 40


def test_select_weekly_keeps_domestic_and_international_quota():
    papers = []
    for index in range(5):
        papers.append(Paper(dedupe_key=f"d{index}", title=f"d{index}", region="domestic", total_score=90 - index))
    for index in range(12):
        papers.append(Paper(dedupe_key=f"i{index}", title=f"i{index}", region="international", total_score=100 - index))

    selected = select_weekly(papers, recommended_keys={"i0", "d0"}, domestic_count=2, international_count=8)

    assert len(selected) == 10
    assert len([paper for paper in selected if paper.region == "domestic"]) == 2
    assert len([paper for paper in selected if paper.region == "international"]) == 8
    assert "i0" not in {paper.dedupe_key for paper in selected}
    assert "d0" not in {paper.dedupe_key for paper in selected}


def test_score_papers_uses_authority_keywords_citations_and_recency():
    cfg = settings()
    papers = [
        Paper(
            dedupe_key="doi:1",
            title="AI misinformation and social media communication",
            abstract="Generative AI, synthetic media and platform algorithm governance.",
            venue="New Media & Society",
            publication_date="2026-06-01",
            citation_count=10,
        ),
        Paper(
            dedupe_key="doi:2",
            title="Unrelated optical communication protocol",
            venue="Unknown Journal",
            publication_date="2026-02-01",
            citation_count=1,
        ),
    ]
    scored = score_papers(papers, cfg, today=date(2026, 6, 17))

    assert scored[0].authority_score == 100
    assert scored[0].keyword_score > scored[1].keyword_score
    assert scored[0].recency_score == 100
    assert scored[0].total_score > scored[1].total_score


def test_quality_filters_remove_book_notes():
    cfg = settings()
    paper = Paper(dedupe_key="book", title="Book notes: The Activation Effect", abstract="social media public opinion")

    assert not passes_quality_filters(paper, cfg)
