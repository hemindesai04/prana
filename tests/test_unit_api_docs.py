from pathlib import Path
import re

from prana.main import create_app

_ENDPOINT_ROW_RE = re.compile(r"^\|\s*`([A-Z]+)`\s*\|\s*`(/[^`]*)`\s*\|")
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _load_architecture_doc() -> str:
    container_path = Path("/ARCHITECTURE.md")
    if container_path.exists():
        return container_path.read_text()
    return (Path(__file__).resolve().parent.parent / "ARCHITECTURE.md").read_text()


def test_architecture_endpoints_match_fastapi_routes():
    """Verify every FastAPI route is documented in ARCHITECTURE.md and vice versa."""
    app = create_app(enable_scheduler=False)
    code_routes = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if method.upper() in _HTTP_METHODS
    }

    doc_text = _load_architecture_doc()
    assert "## 4. HTTP API Endpoints" in doc_text
    section = doc_text.split("## 4. HTTP API Endpoints", 1)[1]
    doc_routes = set()
    for line in section.splitlines():
        m = _ENDPOINT_ROW_RE.match(line.strip())
        if m:
            doc_routes.add((m.group(1), m.group(2)))

    missing_from_docs = code_routes - doc_routes
    stale_in_docs = doc_routes - code_routes
    assert not missing_from_docs, f"Endpoints missing from ARCHITECTURE.md: {sorted(missing_from_docs)}"
    assert not stale_in_docs, f"Stale endpoints in ARCHITECTURE.md not in FastAPI: {sorted(stale_in_docs)}"
