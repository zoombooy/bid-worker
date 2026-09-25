import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from bidreader.config import get_settings
from bidreader.database import SessionLocal
from bidreader.extraction import build_analysis
from bidreader.models import Analysis, Run, RunEvent, Tender
from bidreader.parsers import ParseFailure, parse_document
from bidreader.schemas import AnalysisResult, ProjectField

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bidreader")
_logger = logging.getLogger(__name__)
_lock = Lock()
_scheduled: set[str] = set()


def submit(run_id: str) -> None:
    with _lock:
        if run_id in _scheduled:
            return
        _scheduled.add(run_id)
    _pool.submit(process_run, run_id)


def recover_incomplete() -> None:
    with SessionLocal() as db:
        run_ids = db.scalars(select(Run.id).where(Run.status.in_(["queued", "running"]))).all()
    for run_id in run_ids:
        submit(run_id)


def _event(db: Session, run: Run, stage: str, status: str, message: str) -> None:
    db.add(RunEvent(run_id=run.id, stage=stage, status=status, message=message))


def process_run(run_id: str) -> None:
    try:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None or run.status in {"completed", "needs_review", "failed"}:
                return
            run.status = "running"
            _event(db, run, run.stage, "running", "开始处理招标文件。")
            db.commit()

            parsed = run.stage_state.get("parsed")
            if parsed is None:
                run.stage = "parse"
                _event(db, run, "parse", "running", "识别文档格式并提取结构与原文位置。")
                db.commit()
                config = get_settings()
                parsed = parse_document(Path(run.source_path), max_pages=config.max_pdf_pages,
                                        ocr_enabled=config.local_ocr_enabled, ocr_dpi=config.ocr_dpi)
                run.stage_state = {**run.stage_state, "parsed": parsed}
                run.stage = "extract"
                _event(db, run, "parse", "completed", f"解析完成：{len(parsed['blocks'])} 个内容块。")
                db.commit()

            analysis = build_analysis(run.id, run.filename, parsed)
            needs_review = any(item["state"] != "done" for item in analysis.ledger)
            needs_review = needs_review or bool(analysis.warnings) or bool(analysis.criteria) or bool(analysis.project_fields)
            existing = db.scalar(select(Analysis).where(Analysis.run_id == run.id))
            if existing is None:
                existing = Analysis(run_id=run.id, payload=analysis.model_dump(mode="json"))
                db.add(existing)
            else:
                existing.payload = analysis.model_dump(mode="json")
            run.stage = "review"
            run.status = "needs_review" if needs_review else "completed"
            _event(db, run, "extract", "completed", f"抽取完成：{len(analysis.project_fields)} 个项目字段，{len(analysis.criteria)} 个评分项候选。")
            _event(db, run, "review", "waiting" if needs_review else "completed",
                   "结果需要人工核对。" if needs_review else "文档已处理完成。")
            db.commit()
            tender = db.get(Tender, run.tender_id)
            project_title = next((item.raw_value for item in analysis.project_fields if item.field == "project_title" and item.raw_value), None)
            if project_title and tender:
                tender.title = project_title[:500]
                db.commit()
    except ParseFailure as exc:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.status = "needs_review"
                run.stage = "parse"
                run.error = str(exc)
                document_id = f"source-{run.id}"
                analysis = db.scalar(select(Analysis).where(Analysis.run_id == run.id))
                fallback = AnalysisResult(
                    run_id=run.id,
                    document={"filename": run.filename, "sha256": run.sha256, "document_id": document_id,
                              "pages": [{"page_no": index, "width": None, "height": None} for index in range(1, exc.page_count + 1)],
                              "block_count": 0},
                    project_fields=[ProjectField(field=name, state="failed", review_reason="文档解析失败，不能据此判定该字段不存在。")
                                    for name in ["project_title", "project_number", "submission_deadline", "opening_time", "project_budget", "price_limit", "duration"]],
                    ledger=exc.ledger,
                    warnings=[str(exc)],
                    parser_versions={"document_parser": "failed", "extractor": "not_run"},
                )
                if analysis is None:
                    db.add(Analysis(run_id=run.id, payload=fallback.model_dump(mode="json")))
                else:
                    analysis.payload = fallback.model_dump(mode="json")
                _event(db, run, "parse", "failed_review", str(exc))
                db.commit()
    except Exception as exc:
        _logger.exception("Tender analysis run %s failed", run_id)
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.status = "failed"
                run.error = f"处理失败：{type(exc).__name__}。可从最近保存的任务检查点重试；请查看服务日志。"
                _event(db, run, run.stage, "failed", run.error)
                db.commit()
    finally:
        with _lock:
            _scheduled.discard(run_id)
