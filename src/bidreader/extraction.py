import json
import re
from typing import Any
from uuid import uuid4

import httpx

from bidreader.config import get_settings
from bidreader.schemas import AnalysisResult, Criterion, Evidence, ProjectField, ReviewState

FIELD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("project_title", re.compile(r"(?:招标|采购|建设|工程)?项目名称\s*[：:]\s*(.+)")),
    ("project_number", re.compile(r"(?:招标|采购|项目)?编号\s*[：:]\s*([A-Za-z0-9_./（）()\-—]+)")),
    ("submission_deadline", re.compile(r"(?:投标|响应)(?:文件)?(?:递交|提交|截止)(?:时间)?\s*[：:]?\s*(.{4,60})")),
    ("opening_time", re.compile(r"开标时间\s*[：:]?\s*(.{4,60})")),
    ("project_budget", re.compile(r"(?:项目预算|预算金额)\s*[：:]?\s*([￥¥]?\s*[\d,，.]+\s*(?:万元|万|元)?)")),
    ("price_limit", re.compile(r"(?:最高限价|招标控制价|拦标价|最高投标限价)\s*[：:]?\s*([￥¥]?\s*[\d,，.]+\s*(?:万元|万|元)?)")),
    ("duration", re.compile(r"(?:工期|服务期限|交付期限|项目周期)\s*[：:]?\s*([^。；;\n]{2,80})")),
]

SCORING_MARKERS = re.compile(r"评分|评标办法|评审因素|评审标准|评分标准|分值|得分|价格分")
POINTS = re.compile(r"(?:(?:最高|满|不超过|得|计|加|扣)\s*)?(\d+(?:\.\d+)?)\s*分")
SECTION = re.compile(r"^\s*(?:第[一二三四五六七八九十百\d]+[章节篇]|[一二三四五六七八九十]+、|\d+(?:\.\d+){0,3}[、. ]?)")


def _block_evidence(document_id: str, block: dict[str, Any], quote: str | None = None) -> Evidence:
    return Evidence(
        document_id=document_id,
        page_no=block.get("page_no"),
        block_id=block["block_id"],
        quote=quote or block["text"],
        bbox=block.get("bbox"),
        source_kind=block.get("kind", "text"),
    )


def extract_fields(blocks: list[dict], document_id: str) -> list[ProjectField]:
    found: dict[str, ProjectField] = {}
    for block in blocks:
        if block.get("kind") in {"word"}:
            continue
        text = block.get("text", "").strip()
        for field, pattern in FIELD_RULES:
            if field in found:
                continue
            match = pattern.search(text)
            if not match:
                continue
            raw = match.group(1).strip(" ：:，,。;；")
            if not raw:
                continue
            confidence = 0.93 if field in {"project_number", "price_limit", "project_budget"} else 0.82
            found[field] = ProjectField(
                field=field,
                raw_value=raw,
                normalized_value=None,
                state="candidate",
                confidence=confidence,
                evidence=[_block_evidence(document_id, block, text)],
                review_reason="规则提取结果需人工确认；本阶段不推断未明示的值。",
            )
    ordered = [name for name, _ in FIELD_RULES]
    return [found.get(name, ProjectField(field=name, state="not_found", review_reason="在已解析文本中未发现；需确认相关章节/页面已完整解析。")) for name in ordered]


def _category(text: str, section_path: list[str]) -> tuple[str, str | None]:
    context = " ".join([*section_path, text])
    if re.search(r"价格|报价|投标价|评标价|基准价", context):
        return "price", "price"
    if re.search(r"技术|方案|实施|项目负责人|人员配备|团队|业绩", context):
        return "technical", None
    if re.search(r"商务|财务|资质|信用|荣誉|奖项", context):
        return "commercial", None
    return "unclassified", None


def _subcategory(text: str) -> str | None:
    categories = [
        ("similar_projects", r"业绩|类似工程|类似项目"),
        ("work_plan", r"工作方案|施工组织|实施方案|服务方案|技术方案"),
        ("project_leader", r"项目负责人|项目经理|技术负责人"),
        ("team_staffing", r"工作组|项目组|人员配备|团队人员|拟派人员"),
        ("qualification", r"资质|资格|认证|许可证"),
        ("finance", r"财务|营业收入|纳税|资产负债"),
        ("credit", r"信用|诚信|失信|行政处罚"),
        ("awards", r"奖项|荣誉|获奖"),
    ]
    for name, pattern in categories:
        if re.search(pattern, text):
            return name
    return None


def extract_criteria(blocks: list[dict], document_id: str) -> list[Criterion]:
    criteria: list[Criterion] = []
    in_scoring = False
    active_category = "unclassified"
    for block in blocks:
        text = re.sub(r"\s+", " ", block.get("text", "")).strip()
        if len(text) < 3:
            continue
        path = block.get("section_path", [])
        heading_signal = SCORING_MARKERS.search(text) is not None
        if block.get("kind") == "heading" or SECTION.match(text):
            if heading_signal:
                in_scoring = True
            elif in_scoring and re.search(r"投标文件格式|合同条款|技术规范|采购需求|项目概况|投标人须知", text) and not re.search(r"评分|评审", text):
                in_scoring = False
            if in_scoring:
                active_category = _category(text, path)[0]
            continue
        if heading_signal:
            in_scoring = True
        if not in_scoring:
            continue
        if not re.search(r"分|评分|评审|得分|比例|基准价|报价|业绩|方案|人员|资质|财务|信用|奖项", text):
            continue
        if len(text) < 8:
            continue
        category, _ = _category(text, path)
        if category == "unclassified":
            category = active_category
        point_values = [float(match.group(1)) for match in POINTS.finditer(text)]
        max_score = max(point_values) if point_values else None
        criteria.append(Criterion(
            criterion_id=str(uuid4()),
            category=category,
            subcategory=_subcategory(text),
            source_text=text,
            score_text=text if point_values else None,
            max_score=max_score,
            evidence=[_block_evidence(document_id, block)],
            extraction_state="candidate",
            review_state=ReviewState.PENDING,
            review_reason="评分行候选由关键词和章节定位；须核对上下文、表格列关系及跨页续表。",
        ))
    return criteria


def run_optional_llm_review(result: AnalysisResult) -> None:
    """Optional OpenAI-compatible classification pass; only accepts exact source-grounded quotes."""
    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return
    candidates = [item for item in result.criteria if item.category == "unclassified"]
    if not candidates:
        return
    batch = [{"criterion_id": item.criterion_id, "source_text": item.source_text} for item in candidates[:80]]
    system = (
        "Classify Chinese engineering-tender scoring rows. Return only JSON with key `items`; each item has "
        "criterion_id, category (technical/commercial/price/unclassified), subcategory, source_quote. "
        "Do not invent or paraphrase source_quote. Do not calculate scores or infer missing criteria."
    )
    try:
        response = httpx.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={"model": settings.llm_model, "temperature": 0, "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}]},
            timeout=settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(payload)
        by_id = {candidate.criterion_id: candidate for candidate in candidates}
        for item in parsed.get("items", []):
            target = by_id.get(item.get("criterion_id"))
            quote = item.get("source_quote")
            if target is None or not isinstance(quote, str) or quote not in target.source_text:
                continue
            if item.get("category") not in {"technical", "commercial", "price", "unclassified"}:
                continue
            target.category = item["category"]
            target.subcategory = item.get("subcategory") or target.subcategory
            target.review_reason = "LLM仅辅助分类，引用已按原文子串校验；仍需人工确认。"
    except (httpx.HTTPError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result.warnings.append(f"LLM分类服务不可用，保留规则抽取结果：{type(exc).__name__}")


def build_analysis(run_id: str, filename: str, parsed: dict) -> AnalysisResult:
    document_id = parsed["document_id"]
    fields = extract_fields(parsed["blocks"], document_id)
    criteria = extract_criteria(parsed["blocks"], document_id)
    document = {
        "filename": filename,
        "sha256": parsed["sha256"],
        "document_id": document_id,
        "pages": parsed["pages"],
        "block_count": len(parsed["blocks"]),
        "chunk_count": len(parsed.get("chunks", [])),
        "rendering": "original file; DOCX pagination is renderer-dependent",
        "blocks": parsed["blocks"],
        "chunks": parsed.get("chunks", []),
    }
    result = AnalysisResult(
        run_id=run_id,
        document=document,
        project_fields=fields,
        sections=parsed["sections"],
        criteria=criteria,
        ledger=parsed["ledger"],
        warnings=parsed["warnings"],
        parser_versions={"document_parser": parsed["parser"], "extractor": "rules-v0.1"},
    )
    run_optional_llm_review(result)
    return result
