import json

from fastapi.testclient import TestClient

from bidreader.config import get_settings
from bidreader.mcp_server import get_tender_analysis, search_tender_analysis
from bidreader.models import Analysis, Run, Tender


def test_mcp_analysis_keeps_evidence_and_supports_category_filter(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bidreader.database import Base

    test_engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("bidreader.mcp_server.SessionLocal", TestSession)
    with TestSession() as db:
        tender = Tender(id="t-1", title="样例工程")
        run = Run(id="r-1", tender=tender, filename="sample.docx", sha256="a" * 64,
                  media_type="application/docx", source_path="/tmp/sample.docx", status="needs_review",
                  stage="analysis")
        db.add_all([tender, run])
        db.add(Analysis(run_id="r-1", payload={
            "document": {"filename": "sample.docx", "document_id": "doc-1", "block_count": 1,
                         "blocks": [{"block_id": "b-1", "text": "类似工程业绩每项得2分，最高6分。"}]},
            "project_fields": [], "sections": [],
            "ledger": [{"object_id": "block-1", "kind": "paragraph", "state": "done"},
                       {"object_id": "file-1", "kind": "doc", "state": "failed_review", "reason": "unsupported"}],
            "warnings": [],
            "criteria": [{"criterion_id": "c-1", "category": "technical", "source_text": "类似工程业绩每项得2分，最高6分。",
                          "evidence": [{"document_id": "doc-1", "page_no": 4, "block_id": "b-1",
                                        "quote": "类似工程业绩每项得2分，最高6分。"}],
                          "review_state": "pending"},
                         {"criterion_id": "c-2", "category": "commercial", "source_text": "财务报表得2分。",
                          "evidence": [{"document_id": "doc-1", "page_no": 5, "block_id": "b-2",
                                        "quote": "财务报表得2分。"}],
                          "review_state": "pending"}],
        }))
        db.commit()

    technical = json.loads(get_tender_analysis("t-1", category="technical"))
    assert [item["criterion_id"] for item in technical["criteria"]] == ["c-1"]
    assert technical["criteria"][0]["evidence"][0]["page_no"] == 4
    assert technical["ledger_summary"]["state_counts"] == {"done": 1, "failed_review": 1}
    assert [item["object_id"] for item in technical["ledger_summary"]["review_items"]] == ["file-1"]
    matches = json.loads(search_tender_analysis("t-1", "类似工程业绩", category="technical"))
    assert matches["match_count"] == 1
    assert matches["matches"][0]["evidence"][0]["quote"] == "类似工程业绩每项得2分，最高6分。"
    test_engine.dispose()


def test_mcp_requires_configured_bearer_token():
    from bidreader.mcp_server import mcp_asgi_app

    settings = get_settings()
    original = settings.mcp_auth_token
    settings.mcp_auth_token = "test-secret"
    try:
        with TestClient(mcp_asgi_app, base_url="http://localhost:8001") as client:
            response = client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={})
        assert response.status_code == 401
    finally:
        settings.mcp_auth_token = original
