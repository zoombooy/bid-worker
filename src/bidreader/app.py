import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidreader.config import get_settings
from bidreader.database import Base, SessionLocal, engine, get_session
from bidreader.models import Analysis, ReviewHistory, Run, RunEvent, Tender
from bidreader.parsers import SUPPORTED
from bidreader.schemas import FieldReviewPatch, ReviewPatch, ReviewState, RunConfirmation
from bidreader.worker import recover_incomplete, submit

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "sources").mkdir(parents=True, exist_ok=True)
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    recover_incomplete()
    yield


app = FastAPI(title="BidReader 招标文件解析与评分标准工作台", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=False, allow_methods=["GET", "POST", "PATCH"], allow_headers=["*"])


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^\w.\-（）() ]+", "_", name, flags=re.UNICODE).strip(" .")
    return (name or "tender-document")[:200]


def _run_json(run: Run) -> dict:
    return {"run_id": run.id, "tender_id": run.tender_id, "filename": run.filename,
            "sha256": run.sha256, "status": run.status, "stage": run.stage,
            "error": run.error, "created_at": run.created_at.isoformat()}


def _validate_evidence(document: dict, evidence_items: list[dict], expected_text: str | None = None) -> bool:
    """Only accept evidence whose locator and quote match the stored parsed source block."""
    document_id = document.get("document_id")
    blocks = {block.get("block_id"): block for block in document.get("blocks", [])}
    valid_quotes: list[str] = []
    for evidence in evidence_items:
        block = blocks.get(evidence.get("block_id"))
        quote = evidence.get("quote", "")
        if evidence.get("document_id") != document_id or not block or not quote or quote not in block.get("text", ""):
            continue
        if evidence.get("page_no") != block.get("page_no") or evidence.get("bbox") != block.get("bbox"):
            continue
        if evidence.get("source_file") and evidence.get("source_file") != block.get("source_file"):
            continue
        valid_quotes.append(quote)
    if not valid_quotes:
        return False
    return expected_text is None or any(expected_text in quote for quote in valid_quotes)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "bidreader", "version": app.version}


@app.post("/api/v1/tenders", status_code=202)
async def upload_tender(file: UploadFile = File(...), db: Session = Depends(get_session)):
    filename = _safe_filename(file.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(415, "当前支持 DOCX、PDF、TXT、Markdown。旧版 DOC 需先转换为 DOCX。")
    content = await file.read(settings.max_upload_bytes + 1)
    if not content:
        raise HTTPException(400, "上传文件为空。")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_bytes} bytes。")
    digest = __import__("hashlib").sha256(content).hexdigest()
    tender = Tender(title="待识别项目")
    run_id = str(uuid4())
    source_dir = settings.data_dir / "sources" / run_id
    source_dir.mkdir(parents=True, exist_ok=False)
    source_path = source_dir / filename
    source_path.write_bytes(content)
    run = Run(id=run_id, tender=tender, filename=filename, sha256=digest,
              media_type=file.content_type or "application/octet-stream", source_path=str(source_path))
    db.add_all([tender, run])
    db.add(RunEvent(run_id=run_id, stage="ingest", status="completed", message="文件已安全保存并完成 SHA-256 清点。"))
    db.commit()
    submit(run_id)
    return {"tender_id": tender.id, "run_id": run_id, "sha256": digest, "status": "queued"}


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "解析任务不存在。")
    return _run_json(run)


@app.post("/api/v1/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "解析任务不存在。")
    if run.status != "failed":
        raise HTTPException(409, "仅处理失败的任务可重试；待复核任务应先完成人工核对。")
    if not Path(run.source_path).is_file():
        raise HTTPException(410, "原始文件已不存在，不能从检查点重试。")
    run.status = "queued"
    run.error = None
    db.add(RunEvent(run_id=run_id, stage=run.stage, status="queued", message="已从最近保存的检查点重新排队。"))
    db.commit()
    submit(run_id)
    return {"run_id": run_id, "status": "queued", "resumed_from": run.stage}


@app.get("/api/v1/tenders")
def list_tenders(db: Session = Depends(get_session)):
    tenders = db.scalars(select(Tender).order_by(Tender.created_at.desc()).limit(50)).all()
    result = []
    for tender in tenders:
        runs = db.scalars(select(Run).where(Run.tender_id == tender.id).order_by(Run.created_at.desc())).all()
        result.append({"tender_id": tender.id, "title": tender.title, "created_at": tender.created_at.isoformat(), "runs": [_run_json(run) for run in runs]})
    return result


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, last_event_id: int = Header(default=0, alias="Last-Event-ID"), db: Session = Depends(get_session)):
    if not db.get(Run, run_id):
        raise HTTPException(404, "解析任务不存在。")
    db.close()

    async def stream():
        cursor = max(0, last_event_id)
        while not await request.is_disconnected():
            with SessionLocal() as session:
                rows = session.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > cursor).order_by(RunEvent.id)).all()
                run = session.get(Run, run_id)
                terminal = bool(run and run.status in {"completed", "needs_review", "failed"})
                messages = [f"id: {row.id}\nevent: progress\ndata: {json.dumps({'run_id': run_id, 'stage': row.stage, 'status': row.status, 'message': row.message}, ensure_ascii=False)}\n\n" for row in rows]
            for row, message in zip(rows, messages, strict=False):
                cursor = row.id
                yield message
            if terminal and not rows:
                yield "event: complete\ndata: {}\n\n"
                break
            if not rows:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/tenders/{tender_id}/analysis")
def get_analysis(tender_id: str, run_id: str | None = None, db: Session = Depends(get_session)):
    query = select(Run).where(Run.tender_id == tender_id).order_by(Run.created_at.desc())
    if run_id:
        query = query.where(Run.id == run_id)
    run = db.scalar(query)
    if not run:
        raise HTTPException(404, "未找到招标文件解析结果。")
    analysis = db.scalar(select(Analysis).where(Analysis.run_id == run.id))
    return {"run": _run_json(run), "analysis": analysis.payload if analysis else None}


@app.get("/api/v1/runs/{run_id}/source")
def get_source(run_id: str, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    if not run or not Path(run.source_path).is_file():
        raise HTTPException(404, "原始文件不存在。")
    disposition = "inline" if Path(run.filename).suffix.lower() == ".pdf" else "attachment"
    return FileResponse(run.source_path, filename=run.filename, media_type=run.media_type, content_disposition_type=disposition)


@app.patch("/api/v1/runs/{run_id}/criteria/{criterion_id}")
def update_criterion(run_id: str, criterion_id: str, patch: ReviewPatch, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    analysis = db.scalar(select(Analysis).where(Analysis.run_id == run_id))
    if not run or not analysis:
        raise HTTPException(404, "解析任务或结果不存在。")
    payload = dict(analysis.payload)
    criteria = list(payload.get("criteria", []))
    criterion = next((item for item in criteria if item["criterion_id"] == criterion_id), None)
    if criterion is None:
        raise HTTPException(404, "评分项不存在。")
    before = dict(criterion)
    if patch.source_text is not None:
        if patch.evidence is None:
            raise HTTPException(422, "更改评分项原文时必须同时提交对应的新原文证据。")
        submitted_evidence = [e.model_dump(mode="json") for e in patch.evidence]
        if not _validate_evidence(payload.get("document", {}), submitted_evidence, patch.source_text):
            raise HTTPException(422, "修改后的评分项原文必须完整出现在所引用的原始内容块中。")
        criterion["source_text"] = patch.source_text
    if patch.score_text is not None:
        criterion["score_text"] = patch.score_text
    if patch.max_score is not None:
        criterion["max_score"] = patch.max_score
    if patch.category is not None:
        criterion["category"] = patch.category
    if patch.subcategory is not None:
        criterion["subcategory"] = patch.subcategory
    if patch.evidence is not None:
        criterion["evidence"] = [e.model_dump(mode="json") for e in patch.evidence]
    if patch.action == ReviewState.CONFIRMED:
        evidence = criterion.get("evidence", [])
        if not _validate_evidence(payload.get("document", {}), evidence, criterion.get("source_text", "")):
            raise HTTPException(409, "确认评分项前必须具备能在该评分项原文中逐字核对的证据。")
    criterion["review_state"] = patch.action.value
    criterion["review_reason"] = patch.note
    analysis.payload = {**payload, "criteria": criteria}
    db.add(ReviewHistory(criterion_id=criterion_id, run_id=run_id, reviewer=patch.reviewer,
                         action=patch.action.value, before_value=before, after_value=criterion, note=patch.note))
    _event = RunEvent(run_id=run_id, stage="review", status=patch.action.value, message=f"人工{patch.action.value}评分项 {criterion_id}。")
    db.add(_event)
    db.commit()
    return {"criterion": criterion, "review_event_id": _event.id}


@app.patch("/api/v1/runs/{run_id}/fields/{field_name}")
def update_field(run_id: str, field_name: str, patch: FieldReviewPatch, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    analysis = db.scalar(select(Analysis).where(Analysis.run_id == run_id))
    if not run or not analysis:
        raise HTTPException(404, "解析任务或结果不存在。")
    payload = dict(analysis.payload)
    fields = list(payload.get("project_fields", []))
    field = next((item for item in fields if item["field"] == field_name), None)
    if field is None:
        raise HTTPException(404, "项目字段不存在。")
    before = dict(field)
    if patch.action == ReviewState.CORRECTED:
        field["raw_value"] = patch.raw_value
        field["state"] = "candidate" if patch.raw_value else "not_found"
        if patch.evidence is not None:
            field["evidence"] = [e.model_dump(mode="json") for e in patch.evidence]
        if patch.raw_value and not _validate_evidence(payload.get("document", {}), field.get("evidence", []), patch.raw_value):
            raise HTTPException(422, "更正后的项目字段值必须能在有效原文证据中逐字找到。")
    if (patch.action == ReviewState.CONFIRMED and field.get("raw_value")
            and not _validate_evidence(payload.get("document", {}), field.get("evidence", []), field["raw_value"])):
        raise HTTPException(409, "确认项目字段前，原文证据必须包含该字段值。")
    field["review_state"] = patch.action.value
    field["review_reason"] = patch.note
    analysis.payload = {**payload, "project_fields": fields}
    db.add(ReviewHistory(criterion_id=f"field:{field_name}", run_id=run_id, reviewer=patch.reviewer,
                         action=patch.action.value, before_value=before, after_value=field, note=patch.note))
    _event = RunEvent(run_id=run_id, stage="review", status=patch.action.value, message=f"人工{patch.action.value}项目字段 {field_name}。")
    db.add(_event)
    db.commit()
    return {"field": field, "review_event_id": _event.id}


@app.post("/api/v1/runs/{run_id}/confirm")
def confirm_run(run_id: str, confirmation: RunConfirmation, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    analysis = db.scalar(select(Analysis).where(Analysis.run_id == run_id))
    if not run or not analysis:
        raise HTTPException(404, "解析任务或结果不存在。")
    payload = analysis.payload
    pending_fields = [field["field"] for field in payload.get("project_fields", []) if field.get("review_state") != "confirmed"]
    pending_criteria = [criterion["criterion_id"] for criterion in payload.get("criteria", []) if criterion.get("review_state") not in {"confirmed", "rejected"}]
    incomplete = [item["object_id"] for item in payload.get("ledger", []) if item.get("state") not in {"done", "excluded_with_reason"}]
    if pending_fields or pending_criteria or incomplete:
        raise HTTPException(409, {"message": "仍有项目字段、评分项或文档对象待复核。", "pending_fields": pending_fields,
                                  "pending_criteria": pending_criteria, "incomplete_objects": incomplete})
    if not payload.get("criteria") and not confirmation.confirm_no_scoring_criteria:
        raise HTTPException(409, "未发现评分项候选；请先核对文件全文并显式确认该结果。")
    previous_status = run.status
    run.status = "completed"
    run.stage = "completed"
    db.add(ReviewHistory(criterion_id="run", run_id=run_id, reviewer=confirmation.reviewer, action="confirmed",
                         before_value={"status": previous_status}, after_value={"status": "completed"}, note=confirmation.note))
    db.add(RunEvent(run_id=run_id, stage="review", status="completed", message=f"{confirmation.reviewer}确认解析结果。"))
    db.commit()
    return {"run_id": run_id, "status": run.status, "message": "本次解析结果已人工确认。"}


@app.get("/api/v1/runs/{run_id}/reviews")
def list_reviews(run_id: str, db: Session = Depends(get_session)):
    rows = db.scalars(select(ReviewHistory).where(ReviewHistory.run_id == run_id).order_by(ReviewHistory.created_at)).all()
    return [{"id": row.id, "criterion_id": row.criterion_id, "reviewer": row.reviewer, "action": row.action,
             "before": row.before_value, "after": row.after_value, "note": row.note, "created_at": row.created_at.isoformat()} for row in rows]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
