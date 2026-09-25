from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bidreader.app import _validate_evidence, app, settings
from bidreader.database import Base, get_session
from bidreader.evaluation import aggregate_corpus, evaluate
from bidreader.extraction import extract_criteria, extract_fields
from bidreader.models import Analysis, Run, RunEvent
from bidreader.parsers import _ocr_page, parse_document, parse_docx


def _docx_bytes() -> bytes:
    document = Document()
    document.add_heading("第一章 项目概况", level=1)
    document.add_paragraph("项目名称：城市供水设施改造工程")
    document.add_paragraph("招标编号：ZB-2026-015")
    document.add_heading("第二章 评标办法", level=1)
    document.add_paragraph("技术评分标准")
    document.add_paragraph("类似工程业绩：每提供一项得 2 分，最高 6 分。")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "项目负责人"
    table.cell(0, 1).text = "具备高级工程师职称得 4 分"
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _blocks():
    return [
        {"block_id": "b-0", "kind": "heading", "text": "评标办法", "page_no": 3, "bbox": None, "section_path": ["评标办法"], "source_index": 0},
        {"block_id": "b-1", "kind": "line", "text": "技术评分标准", "page_no": 3, "bbox": None, "section_path": ["评标办法"], "source_index": 1},
        {"block_id": "b-2", "kind": "table_row", "text": "相似工程业绩 | 每提供一项得 2 分，最高 6 分", "page_no": 4, "bbox": [30, 40, 400, 80], "section_path": ["评标办法", "技术评分标准"], "source_index": 2, "table_id": "t-1", "row_index": 2},
        {"block_id": "b-3", "kind": "heading", "text": "投标文件格式", "page_no": 15, "bbox": None, "section_path": ["投标文件格式"], "source_index": 3},
        {"block_id": "b-4", "kind": "line", "text": "未响应投标文件格式的，不予受理", "page_no": 16, "bbox": None, "section_path": ["投标文件格式"], "source_index": 4},
    ]


def test_docx_preserves_body_order_headings_and_table_rows(tmp_path):
    path = tmp_path / "sample.docx"
    path.write_bytes(_docx_bytes())

    blocks, sections = parse_docx(path)

    texts = [block.text for block in blocks]
    assert texts.index("项目名称：城市供水设施改造工程") < texts.index("技术评分标准")
    assert any(block.kind == "table_row" and "项目负责人" in block.text for block in blocks)
    assert any(section["title"] == "第一章 项目概况" for section in sections)


def test_project_fields_are_grounded_and_not_invented():
    fields = extract_fields(_blocks(), "doc-1")
    by_name = {field.field: field for field in fields}

    # The test blocks deliberately contain no project identifier or schedule.
    assert by_name["project_title"].state == "not_found"
    assert by_name["project_number"].state == "not_found"
    assert by_name["price_limit"].normalized_value is None


def test_project_field_candidate_carries_exact_source_evidence():
    blocks = [{"block_id": "b-title", "kind": "line", "text": "项目名称：城市供水设施改造工程",
              "page_no": 2, "bbox": [30, 40, 300, 55], "section_path": ["项目概况"], "source_index": 0}]

    fields = {item.field: item for item in extract_fields(blocks, "doc-2")}

    assert fields["project_title"].raw_value == "城市供水设施改造工程"
    assert fields["project_title"].evidence[0].quote == blocks[0]["text"]
    assert fields["project_title"].evidence[0].page_no == 2


def test_evidence_validation_rejects_forged_page_or_block_quote():
    document = {"document_id": "doc-1", "blocks": [
        {"block_id": "b-1", "text": "项目名称：样例工程", "page_no": 3, "bbox": [10, 20, 90, 40]}]}
    evidence = [{"document_id": "doc-1", "page_no": 3, "block_id": "b-1", "quote": "样例工程",
                 "bbox": [10, 20, 90, 40]}]

    assert _validate_evidence(document, evidence, "样例工程")
    assert not _validate_evidence(document, [{**evidence[0], "page_no": 4}], "样例工程")
    assert not _validate_evidence(document, [{**evidence[0], "quote": "虚构工程"}], "虚构工程")


def test_paddle_ocr_boxes_map_back_to_pdf_points(monkeypatch):
    class FakeResult:
        def __init__(self):
            self.json = {"res": {"rec_texts": ["最高限价"], "rec_boxes": [[20, 40, 180, 80]], "rec_scores": [0.98]}}

    class FakePipeline:
        def predict(self, _image):
            return [FakeResult()]

    monkeypatch.setattr("bidreader.parsers._paddle_ocr", lambda: FakePipeline())
    blocks = _ocr_page(Image.new("RGB", (360, 720)), 2, 180, 360, 7)

    assert blocks[0].text == "最高限价"
    assert blocks[0].bbox == [10, 20, 90, 40]
    assert blocks[0].page_no == 2


def test_gold_evaluation_reports_recall_precision_score_and_citation_accuracy():
    digest = "a" * 64
    gold = {
        "schema_version": "1.0",
        "annotation_complete": True,
        "document": {"sha256": digest},
        "coverage_reviewed": ["project_fields", "technical_criteria", "commercial_criteria", "price_criteria"],
        "project_fields": {"project_title": {"value": "示例 工程"}, "project_number": {"value": None}},
        "criteria": [{"category": "technical", "source_quote": "类似工程业绩每项得2分最高6分。",
                      "page_no": 8, "max_score": 6, "bbox": [10, 20, 100, 40]}],
    }
    prediction = {"analysis": {
        "document": {"sha256": digest},
        "project_fields": [{"field": "project_title", "raw_value": "示例工程"},
                           {"field": "project_number", "raw_value": None}],
        "criteria": [
            {"criterion_id": "hit", "category": "technical", "source_text": "类似工程业绩每项得2分最高6分。",
             "max_score": 6, "evidence": [{"page_no": 8, "quote": "类似工程业绩每项得2分最高6分。",
                                            "bbox": [10, 20, 100, 40]}]},
            {"criterion_id": "extra", "category": "commercial", "source_text": "企业信用评价加3分",
             "max_score": 3, "evidence": [{"page_no": 12, "quote": "企业信用评价加3分"}]},
        ],
    }}

    report = evaluate(gold, prediction)

    assert report["metrics"]["project_field_exact_accuracy"] == 1.0
    assert report["metrics"]["criterion_precision"] == 0.5
    assert report["metrics"]["criterion_recall"] == 1.0
    assert report["metrics"]["criterion_max_score_accuracy"] == 1.0
    assert report["metrics"]["evidence_quote_and_page_accuracy"] == 1.0
    assert report["metrics"]["bbox_iou_at_least_0_5_accuracy"] == 1.0
    corpus = aggregate_corpus([{"project_id": "p-1", "split": "blind_test", "format": "scanned_pdf",
                               "report": report}])
    assert corpus["by_split"]["blind_test"]["metrics"]["criterion_f1"] == report["metrics"]["criterion_f1"]
    assert corpus["by_format"]["scanned_pdf"]["samples"] == 1


def test_corpus_evaluation_rejects_project_leakage_across_splits():
    sample = {"project_id": "same-project", "split": "development", "report": {"counts": {}}}
    duplicate = {**sample, "split": "blind_test"}

    try:
        aggregate_corpus([sample, duplicate])
    except ValueError as exc:
        assert "项目级泄漏" in str(exc)
    else:
        raise AssertionError("the corpus evaluator must reject project leakage between splits")


def test_gold_evaluation_rejects_wrong_document_version():
    gold = {"schema_version": "1.0", "annotation_complete": True,
            "document": {"sha256": "a" * 64},
            "coverage_reviewed": ["project_fields", "technical_criteria", "commercial_criteria", "price_criteria"],
            "project_fields": {"project_title": None}, "criteria": []}

    try:
        evaluate(gold, {"document": {"sha256": "b" * 64}, "project_fields": [], "criteria": []})
    except ValueError as exc:
        assert "SHA-256" in str(exc)
    else:
        raise AssertionError("evaluation should reject predictions from a different source file version")


def test_scoring_candidate_keeps_source_and_evidence_without_assumed_total():
    criteria = extract_criteria(_blocks(), "doc-1")

    assert len(criteria) == 1
    candidate = criteria[0]
    assert candidate.category == "technical"
    assert candidate.subcategory == "similar_projects"
    assert candidate.max_score == 6
    assert candidate.source_text == _blocks()[2]["text"]
    assert candidate.evidence[0].page_no == 4
    assert candidate.evidence[0].bbox == [30, 40, 400, 80]
    assert "不予受理" not in candidate.source_text


def test_text_upload_parser_records_hash_and_anchor(tmp_path):
    path = tmp_path / "tender.txt"
    path.write_text("项目名称：示例工程\n评标办法\n商务评分 最高 3 分", encoding="utf-8")

    parsed = parse_document(path, max_pages=100)

    assert len(parsed["sha256"]) == 64
    assert parsed["parser"] == "utf-8-text"
    assert parsed["blocks"][0]["text"] == "项目名称：示例工程"
    assert all(item["state"] == "done" for item in parsed["ledger"])


def test_upload_endpoint_persists_task_and_review_uses_history(tmp_path, monkeypatch):
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_session():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("bidreader.app.submit", lambda _run_id: None)
    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/tenders", files={"file": ("tender.docx", _docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
            assert response.status_code == 202
            ids = response.json()
            with TestSession() as session:
                run = session.get(Run, ids["run_id"])
                assert run is not None and run.sha256 == ids["sha256"]
                assert session.scalar(select(RunEvent).where(RunEvent.run_id == run.id)) is not None
                run.status = "failed"
                session.commit()
            retried = client.post(f"/api/v1/runs/{ids['run_id']}/retry")
            assert retried.status_code == 202
            assert retried.json()["status"] == "queued"
            with TestSession() as session:
                run = session.get(Run, ids["run_id"])
                run.status = "needs_review"
                payload = {"run_id": run.id, "document": {"document_id": "d-1", "blocks": [
                    {"block_id": "b-1", "text": "原文评分条款", "page_no": 1, "bbox": None}]}, "project_fields": [], "sections": [], "criteria": [
                    {"criterion_id": "c-1", "category": "technical", "subcategory": None, "source_text": "原文评分条款", "score_text": None,
                     "max_score": 3, "tiers": [], "evidence_required": [], "conditions": [], "formula_text": None,
                     "evidence": [{"document_id": "d-1", "page_no": 1, "block_id": "b-1", "quote": "原文评分条款", "bbox": None, "source_kind": "text"}],
                     "extraction_state": "candidate", "review_state": "pending", "review_reason": None}], "ledger": [], "warnings": [], "parser_versions": {}}
                session.add(Analysis(run_id=run.id, payload=payload))
                session.commit()
            reviewed = client.patch(f"/api/v1/runs/{ids['run_id']}/criteria/c-1", json={"action": "confirmed", "reviewer": "tester", "note": "source checked"})
            assert reviewed.status_code == 200
            history = client.get(f"/api/v1/runs/{ids['run_id']}/reviews")
            assert history.status_code == 200
            assert history.json()[0]["action"] == "confirmed"
            assert history.json()[0]["before"]["review_state"] == "pending"
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()
