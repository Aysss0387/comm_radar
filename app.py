import os
import shutil
import tempfile
from pathlib import Path

from comm_paper_radar.research_web import create_app


def prepare_runtime_base(source_base: Path) -> Path:
    if not os.environ.get("VERCEL"):
        return source_base

    runtime_base = Path(tempfile.mkdtemp(prefix="comm-radar-"))
    for directory_name in ("web", "research", "data"):
        source = source_base / directory_name
        destination = runtime_base / directory_name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.mkdir(parents=True, exist_ok=True)
    return runtime_base


app = create_app(prepare_runtime_base(Path(__file__).resolve().parent))
