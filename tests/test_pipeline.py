from pathlib import Path

from comm_paper_radar.config import load_settings
from comm_paper_radar.models import Paper
from comm_paper_radar.pipeline import run_search, run_weekly
from comm_paper_radar.storage import load_papers, load_recommended_keys


class FakeClient:
    def fetch_openalex(self, *args, **kwargs):
        papers = []
        for index in range(4):
            papers.append(
                Paper(
                    dedupe_key=f"domestic:{index}",
                    title=f"人工智能反诈传播研究 {index}",
                    abstract="社交媒体 反诈 人工智能 风险传播",
                    venue="新闻与传播研究",
                    publication_date="2026-06-01",
                    citation_count=10 + index,
                    institution_countries=["CN"],
                    openalex_url=f"https://openalex.org/WCN{index}",
                )
            )
        for index in range(12):
            papers.append(
                Paper(
                    dedupe_key=f"intl:{index}",
                    title=f"AI misinformation and online fraud communication {index}",
                    abstract="social media generative AI phishing scam risk communication",
                    venue="New Media & Society",
                    publication_date="2026-06-01",
                    citation_count=20 + index,
                    openalex_url=f"https://openalex.org/W{index}",
                )
            )
        return papers

    def fetch_crossref(self, *args, **kwargs):
        return [
            Paper(
                dedupe_key="doi:10.fake/extra",
                title="Deepfake trust and platform algorithm communication",
                abstract="AI-mediated communication and synthetic media.",
                venue="Journal of Communication",
                publication_date="2026-05-15",
                citation_count=5,
                doi="10.fake/extra",
            )
        ]

    def enrich_semantic_scholar(self, papers, batch_size=100):
        return papers

    def backfill_abstracts(self, papers, delay_seconds=0.0):
        return papers


def test_weekly_generates_report_csv_and_recommendation_history(tmp_path: Path):
    cfg = load_settings("config/settings.yml")

    selected = run_weekly(cfg, tmp_path, client=FakeClient(), from_date="2026-02-17", to_date="2026-06-17")

    assert len(selected) == 10
    assert len([paper for paper in selected if paper.region == "domestic"]) == 2
    assert len([paper for paper in selected if paper.region == "international"]) == 8
    assert (tmp_path / "data" / "papers.csv").exists()
    assert (tmp_path / "data" / "recommended.csv").exists()
    assert list((tmp_path / "reports").glob("*.md"))

    before_keys = load_recommended_keys(tmp_path / "data" / "recommended.csv")
    second = run_weekly(cfg, tmp_path, client=FakeClient(), from_date="2026-02-17", to_date="2026-06-17")
    after_keys = load_recommended_keys(tmp_path / "data" / "recommended.csv")

    assert before_keys <= after_keys
    assert len(second) == 7


def test_manual_search_updates_library_without_marking_recommended(tmp_path: Path):
    cfg = load_settings("config/settings.yml")
    report = tmp_path / "reports" / "manual" / "ai.md"

    selected = run_search(
        cfg,
        tmp_path,
        client=FakeClient(),
        profile="ai_communication",
        keywords="deepfake trust",
        region="all",
        from_date="2026-02-17",
        to_date="2026-06-17",
        top=5,
        report_path=report,
    )

    assert len(selected) == 5
    assert report.exists()
    assert len(load_papers(tmp_path / "data" / "papers.csv")) >= 5
    assert not (tmp_path / "data" / "recommended.csv").exists()

