"""Read-only MCP tools for Yuxi and other MCP-compatible agents."""

import hmac
import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from bidreader.config import get_settings
from bidreader.database import SessionLocal
from bidreader.models import Analysis, Run, RunEvent, Tender

mcp = FastMCP(
    "BidReader 招标文件解析",
    instructions=(
        "查询已上传到 BidReader 的招标文件解析结果。所有抽取结果均为待核候选，"
        "回答时保留评分原文和 evidence 引用；不得把候选当成已确认事实，也不得补写未找到的内容。"
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["bidreader-mcp:8001", "localhost:8001", "127.0.0.1:8001"]
    ),
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request):
    return JSONResponse({"status": "ok", "service": "bidreader-mcp"})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@mcp.tool()
def list_tenders(limit: int = 10) -> str:
    """列出最近上传的招标文件、解析任务状态及可用于后续查询的 ID。"""
    limit = max(1, min(limit, 50))
    with SessionLocal() as db:
        tenders = db.scalars(select(Tender).order_by(Tender.created_at.desc()).limit(limit)).all()
        result = []
        for tender in tenders:
            runs = db.scalars(
                select(Run).where(Run.tender_id == tender.id).order_by(Run.created_at.desc())
            ).all()
            result.append({
                "tender_id": tender.id,
                "title": tender.title,
                "created_at": tender.created_at.isoformat(),
                "runs": [{
                    "run_id": run.id,
                    "filename": run.filename,
                    "status": run.status,
                    "stage": run.stage,
                    "error": run.error,
                } for run in runs],
            })
    return _json(result)


@mcp.tool()
def get_run_status(run_id: str) -> str:
    """查询单个解析任务状态、当前阶段和最近进度事件。"""
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            return _json({"error": "run_not_found", "run_id": run_id})
        events = db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id)
            .order_by(RunEvent.id.desc()).limit(20)
        ).all()
        return _json({
            "run_id": run.id,
            "tender_id": run.tender_id,
            "filename": run.filename,
            "status": run.status,
            "stage": run.stage,
            "error": run.error,
            "events": [{"stage": event.stage, "status": event.status,
                        "message": event.message, "created_at": event.created_at.isoformat()}
                       for event in reversed(events)],
        })


@mcp.tool()
def get_tender_analysis(tender_id: str, run_id: str = "", category: str = "") -> str:
    """读取结构化分析。category 可传 technical、commercial、price 或 unclassified。"""
    allowed = {"", "technical", "commercial", "price", "unclassified"}
    if category not in allowed:
        return _json({"error": "invalid_category", "allowed": sorted(allowed)})
    with SessionLocal() as db:
        query = select(Run).where(Run.tender_id == tender_id).order_by(Run.created_at.desc())
        if run_id:
            query = query.where(Run.id == run_id)
        run = db.scalar(query)
        if run is None:
            return _json({"error": "tender_or_run_not_found", "tender_id": tender_id,
                           "run_id": run_id or None})
        analysis = db.scalar(select(Analysis).where(Analysis.run_id == run.id))
        if analysis is None:
            return _json({"run_id": run.id, "status": run.status, "stage": run.stage,
                           "message": "解析结果尚未生成，请先查询任务状态。"})
        payload = analysis.payload
        criteria = payload.get("criteria", [])
        if category:
            criteria = [item for item in criteria if item.get("category") == category]
        ledger = payload.get("ledger", [])
        ledger_counts: dict[str, int] = {}
        for item in ledger:
            state = item.get("state", "unknown")
            ledger_counts[state] = ledger_counts.get(state, 0) + 1
        review_items = [item for item in ledger if item.get("state") != "done"]
        pages = payload.get("document", {}).get("pages", [])
        sections = payload.get("sections", [])
        return _json({
            "run": {"run_id": run.id, "tender_id": run.tender_id, "filename": run.filename,
                    "status": run.status, "stage": run.stage},
            "document": {key: payload.get("document", {}).get(key) for key in
                         ("filename", "sha256", "document_id", "scope_hint", "source_documents",
                          "block_count", "chunk_count")} | {"page_count": len(pages)},
            "project_fields": payload.get("project_fields", []),
            "sections": sections[:200],
            "sections_truncated": len(sections) > 200,
            "criteria": criteria,
            "ledger_summary": {"total": len(ledger), "state_counts": ledger_counts,
                               "review_items": review_items[:100], "review_items_truncated": len(review_items) > 100},
            "warnings": payload.get("warnings", [])[:40],
            "review_guidance": "结果均为候选；按 evidence 中的文件、页码、区域、原文引用逐项核验。",
        })


@mcp.tool()
def search_tender_analysis(tender_id: str, query: str, category: str = "", limit: int = 10) -> str:
    """在已提取的项目字段和评分项中按原文检索，并返回命中项及其证据定位。"""
    term = query.strip()
    if not term:
        return _json({"error": "query_required"})
    limit = max(1, min(limit, 30))
    full = json.loads(get_tender_analysis(tender_id, category=category))
    if full.get("error"):
        return _json(full)
    matches = []
    for field in full.get("project_fields", []):
        haystack = " ".join(str(field.get(key) or "") for key in ("field", "raw_value", "normalized_value"))
        if term.casefold() in haystack.casefold():
            matches.append({"kind": "project_field", **field})
    for criterion in full.get("criteria", []):
        haystack = " ".join(str(criterion.get(key) or "") for key in
                             ("category", "criterion_label", "subcategory", "source_text", "score_text", "formula_text"))
        haystack += " " + " ".join(str(value) for value in criterion.get("conditions", []))
        if term.casefold() in haystack.casefold():
            matches.append({"kind": "scoring_criterion", **criterion})
    return _json({"tender_id": tender_id, "query": term, "matches": matches[:limit],
                  "match_count": len(matches), "truncated": len(matches) > limit})


mcp_asgi_app = mcp.streamable_http_app()


class BearerTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
            token = get_settings().mcp_auth_token
            supplied = request.headers.get("authorization", "")
            if not token:
                return JSONResponse(status_code=503, content={"detail": "MCP_AUTH_TOKEN is not configured."})
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                return JSONResponse(status_code=401, content={"detail": "Invalid MCP bearer token."},
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


mcp_asgi_app.add_middleware(BearerTokenMiddleware)
