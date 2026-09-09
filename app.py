from pathlib import Path

from comm_paper_radar.research_web import create_app


app = create_app(Path(__file__).resolve().parent)
