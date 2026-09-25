import json
import re
from functools import lru_cache
from typing import Any
from uuid import uuid4

import httpx

from bidreader.config import get_settings
from bidreader.schemas import AnalysisResult, Criterion, Evidence, ProjectField, ReviewState
from bidreader.vendor.tender_extract.extraction_engine import ExtractionEngine

FIELD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("project_title", re.compile(r"(?:招标|采购|建设|工程)?项目名称\s*[：:]\s*(.+)")),
    ("project_number", re.compile(r"(?:招标|采购|项目)?编号\s*[：:]\s*([A-Za-z0-9_./（）()\-—]+)")),
    ("submission_deadline", re.compile(r"(?:投标文件递交（提交）的截止时间|投标截止时间|响应文件截止时间)[^\n。；;]{0,80}?(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日(?:[^\n。；;]{0,20})?)")),
    ("opening_time", re.compile(r"开标时间[^\n。；;]{0,50}?(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日(?:[^\n。；;]{0,20})?)")),
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
        source_file=block.get("source_file"),
        page_no=block.get("page_no"),
        sheet_name=block.get("sheet_name"),
        cell_range=block.get("cell_range"),
        block_id=block["block_id"],
        quote=quote or block["text"],
        bbox=block.get("bbox"),
        source_kind=block.get("kind", "text"),
    )


def _valid_field_value(field: str, value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if field == "project_title":
        return (
            len(value) <= 80
            and "|" not in value
            and not re.search(r"项目名称|招标编号|招标文件|投标函|见.*前附表|详见|愿以|全部内容|踏勘时间|踏勘地点|姓名|电话|工程名称|委托方|承包人|甲方|乙方", value)
            and not value.startswith(("，", ",", "：", ":"))
        )
    if field in {"submission_deadline", "opening_time"}:
        return re.search(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日", value) is not None
    if field == "duration":
        return (
            not re.search(r"见.*(?:技术规范书|工程量清单|招标公告)|以.*为准", value)
            and re.search(r"\d+\s*(?:日历天|天|日|周|个月|月|年)", value) is not None
        )
    return True


@lru_cache(maxsize=1)
def _upstream_engine() -> ExtractionEngine:
    return ExtractionEngine()


def _upstream_project_fields(blocks: list[dict], document_id: str) -> dict[str, ProjectField]:
    """Run MIT-licensed tender-extract patterns and bind their spans to our source blocks."""
    searchable = [block for block in blocks if block.get("kind") not in {"word", "table_cell"}]
    offsets: list[tuple[int, int, dict]] = []
    parts: list[str] = []
    cursor = 0
    for block in searchable:
        text = block.get("text", "").strip()
        if not text:
            continue
        if parts:
            cursor += 1
            parts.append("\n")
        start = cursor
        parts.append(text)
        cursor += len(text)
        offsets.append((start, cursor, block))
    content = "".join(parts)
    extracted = _upstream_engine().extract_all_fields(
        content, ["project_name", "project_number", "bid_amount"]
    )
    mapping = {"project_name": "project_title", "project_number": "project_number"}
    candidates: dict[str, ProjectField] = {}
    for upstream_name in ("project_name", "project_number", "bid_amount"):
        upstream_field = extracted.get(upstream_name)
        if not upstream_field or not upstream_field.values:
            continue
        spans = sorted(upstream_field.values, key=lambda item: item.confidence, reverse=True)
        for span in spans:
            value = span.value.strip()
            if upstream_name == "project_name" and (
                re.search(r"见.*(?:前附表|招标公告)|详见|以.*为准|招标编号|项目名称", value)
                or "|" in value
            ):
                continue
            block = next((item for start, end, item in offsets
                          if start <= span.start < end and value in item.get("text", "")), None)
            if block is None:
                block = next((item for _, _, item in offsets if value and value in item.get("text", "")), None)
            if block is None:
                continue
            field_name = mapping.get(upstream_name)
            if upstream_name == "bid_amount":
                block_text = block.get("text", "")
                limit_label = re.search(r"最高投标限价|最高限价|招标控制价|拦标价", block_text)
                budget_label = re.search(r"项目预算|预算金额", block_text)
                if bool(limit_label) == bool(budget_label):
                    continue
                field_name = "price_limit" if limit_label else "project_budget"
            if field_name is None:
                continue
            if not _valid_field_value(field_name, value):
                continue
            alternatives = list(dict.fromkeys(item.value for item in spans))
            reason = "由 Inupedia/tender-extract 的增强规则抽取；必须核对原文证据。"
            if len(alternatives) > 1:
                reason += f" 检测到多个不同候选：{'；'.join(alternatives)}。"
            candidates[field_name] = ProjectField(
                field=field_name,
                raw_value=value,
                normalized_value=span.normalized_value,
                state="candidate",
                confidence=span.confidence,
                evidence=[_block_evidence(document_id, block)],
                review_reason=reason,
            )
            break
    return candidates


def extract_fields(blocks: list[dict], document_id: str) -> list[ProjectField]:
    found: dict[str, ProjectField] = _upstream_project_fields(blocks, document_id)
    for block in blocks:
        if block.get("kind") in {"word"}:
            continue
        text = block.get("text", "").strip()
        for field, pattern in FIELD_RULES:
            if field in found:
                continue
            if block.get("kind") == "table_row" and field in {
                "project_title", "submission_deadline", "opening_time", "duration"
            }:
                continue
            match = pattern.search(text)
            if not match:
                continue
            raw = match.group(1).strip(" ：:，,。;；")
            if not raw:
                continue
            if not _valid_field_value(field, raw):
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
    if "project_title" not in found:
        for block in blocks:
            match = re.search(r"(?:^|\s)\d*\s*(结算审核\s*包\s*\d+)(?:\s|$)", block.get("text", ""))
            if match:
                value = re.sub(r"\s+", "", match.group(1))
                found["project_title"] = ProjectField(
                    field="project_title",
                    raw_value=value,
                    state="candidate",
                    confidence=0.9,
                    evidence=[_block_evidence(document_id, block)],
                    review_reason="从技术规范书标题识别到包件名称；需与招标公告对应关系一并核对。",
                )
                break
    ordered = [name for name, _ in FIELD_RULES]
    return [found.get(name, ProjectField(field=name, state="not_found", review_reason="在已解析文本中未发现；需确认相关章节/页面已完整解析。")) for name in ordered]


def _package_specific_fields(blocks: list[dict], document_id: str, scope_hint: str) -> list[ProjectField]:
    package_match = re.search(r"结算审核\s*包\s*(\d+)", scope_hint)
    if not package_match:
        return []
    package_no = package_match.group(1)
    grouped: dict[str, list[dict]] = {}
    for block in blocks:
        if block.get("kind") == "table_row" and block.get("table_id"):
            grouped.setdefault(block["table_id"], []).append(block)
    selected: list[dict] = []
    for rows in grouped.values():
        headers = [re.sub(r"\s+", "", cell) for cell in rows[0].get("cells", [])]
        package_index = next((i for i, value in enumerate(headers) if value == "包号"), None)
        project_index = next((i for i, value in enumerate(headers) if value == "工程名称"), None)
        name_index = next((i for i, value in enumerate(headers) if value in {"包名称", "标包名称"}), None)
        if None in {package_index, project_index, name_index}:
            continue
        limit_index = next((i for i, value in enumerate(headers) if "最高限价" in value), None)
        start_index = next((i for i, value in enumerate(headers) if "计划开工" in value or value.startswith("开始时间")), None)
        end_index = next((i for i, value in enumerate(headers) if "计划竣工" in value or value.startswith("完成时间")), None)
        duration_index = next((i for i, value in enumerate(headers) if value in {"工期要求", "服务期限", "计划工期"}), None)
        for row in rows[1:]:
            cells = row.get("cells", [])
            if max(package_index, project_index, name_index) >= len(cells):
                continue
            if not re.fullmatch(rf"包\s*0*{package_no}", cells[package_index].strip()):
                continue
            if "结算审核" not in cells[name_index]:
                continue
            selected.append({"block": row, "cells": cells, "project_index": project_index,
                             "name_index": name_index, "limit_index": limit_index,
                             "start_index": start_index, "end_index": end_index,
                             "duration_index": duration_index,
                             "start_label": headers[start_index] if start_index is not None else "",
                             "end_label": headers[end_index] if end_index is not None else ""})
    if not selected:
        return []
    source_values: dict[str, list[ProjectField]] = {"project_title": [], "package_name": [], "price_limit": [], "duration": []}
    for item in selected:
        row, cells = item["block"], item["cells"]

        def add(field: str, value: str, normalized: str | None = None, evidence_block: dict = row) -> None:
            if value.strip():
                source_values[field].append(ProjectField(
                    field=field, raw_value=value.strip(), normalized_value=normalized,
                    state="candidate", confidence=0.98, evidence=[_block_evidence(document_id, evidence_block)],
                    review_reason="由包号、结算审核包名和公告包件表字段交叉定位；仍需人工确认。",
                ))

        add("project_title", cells[item["project_index"]])
        add("package_name", cells[item["name_index"]])
        limit_index = item["limit_index"]
        if limit_index is not None and limit_index < len(cells):
            raw_limit = cells[limit_index].strip()
            number = re.sub(r"[^\d.\-]", "", raw_limit)
            normalized = f"{float(number) * 10000:.2f}" if number else None
            add("price_limit", f"{raw_limit}万元" if raw_limit and "万元" not in raw_limit else raw_limit, normalized)
        start_index, end_index, duration_index = item["start_index"], item["end_index"], item["duration_index"]
        if start_index is not None and end_index is not None and max(start_index, end_index) < len(cells):
            start_label = "开始时间" if item["start_label"].startswith("开始时间") else "计划开工"
            end_label = "完成时间" if item["end_label"].startswith("完成时间") else "计划竣工"
            add("duration", f"{start_label} {cells[start_index]} 至 {end_label} {cells[end_index]}")
        elif duration_index is not None and duration_index < len(cells):
            add("duration", cells[duration_index])
    output: list[ProjectField] = []
    for field, candidates in source_values.items():
        if not candidates:
            continue
        unique = {candidate.raw_value: candidate for candidate in candidates}
        if len(unique) == 1:
            output.append(next(iter(unique.values())))
        else:
            evidence = [item.evidence[0] for item in candidates]
            output.append(ProjectField(
                field=field, state="conflict", evidence=evidence,
                review_reason=f"同一包件在公告中出现多个不同候选值（{len(unique)} 个）；需人工判定。",
            ))
    return output


def _category(text: str, section_path: list[str]) -> tuple[str, str | None]:
    context = " ".join([*section_path, text])
    if re.search(r"价格|报价|投标价|评标价|基准价", context):
        return "price", "price"
    if re.search(r"技术|方案|实施|项目负责人|人员配备|团队|业绩", context):
        return "technical", None
    if re.search(r"商务|财务|资质|信用|信誉|荣誉|奖项", context):
        return "commercial", None
    return "unclassified", None


def _subcategory(text: str) -> str | None:
    categories = [
        ("project_leader", r"项目负责人|项目经理|技术负责人"),
        ("team_staffing", r"工作组|项目组|人员配备|团队人员|拟派人员|组织机构|人员配置|团队履责"),
        ("work_plan", r"工作方案|施工组织|实施方案|服务方案|技术方案"),
        ("similar_projects", r"业绩|类似工程|类似项目"),
        ("qualification", r"资质|资格|认证|许可证"),
        ("finance", r"财务|营业收入|纳税|资产负债"),
        ("credit", r"信用|诚信|失信|行政处罚"),
        ("awards", r"奖项|荣誉|获奖"),
    ]
    for name, pattern in categories:
        if re.search(pattern, text):
            return name
    return None


def extract_criteria(blocks: list[dict], document_id: str, scope_hint: str | None = None) -> list[Criterion]:
    criteria: list[Criterion] = []
    grouped: dict[str, list[dict]] = {}
    for block in blocks:
        table_id = block.get("table_id")
        if block.get("kind") == "table_row" and table_id:
            grouped.setdefault(table_id, []).append(block)

    # Only treat rows as scoring criteria when the source table has an explicit
    # scoring header. Mentions of "评分" elsewhere in tender boilerplate are not criteria.
    scoring_tables: set[str] = set()
    price_tables: set[str] = set()
    for table_id, rows in grouped.items():
        headers = [" ".join(row.get("cells", []) or [row.get("text", "")]) for row in rows[:3]]
        if any("评审内容及分值" in header and "项目内容" in header for header in headers):
            scoring_tables.add(table_id)
        elif any(re.search(r"2\.2\.4\s*（?3）?\s*投标报价评分标准", header) for header in headers):
            price_tables.add(table_id)

    active_score_category = "unclassified"
    table_categories: dict[str, str] = {}
    table_contexts: dict[str, str] = {}
    recent_context: list[str] = []
    for block in blocks:
        if block.get("kind") != "table_row":
            text = block.get("text", "")
            if text.strip():
                recent_context.append(text.strip())
                recent_context = recent_context[-8:]
            if re.search(r"评标办法前附表之三\s*[：:]?\s*商务评分标准|商务评分标准", text):
                active_score_category = "commercial"
            elif re.search(r"评标办法前附表之四\s*[：:]?\s*技术评分标准|技术评分标准", text):
                active_score_category = "technical"
            elif re.search(r"评标办法前附表之五\s*[：:]?\s*价格评分标准|投标报价评分标准", text):
                active_score_category = "price"
        elif block.get("row_index") == 0 and block.get("table_id") in scoring_tables | price_tables:
            table_categories[block["table_id"]] = "price" if block["table_id"] in price_tables else active_score_category
            table_contexts[block["table_id"]] = " ".join(recent_context)
            recent_context.clear()

    settlement_package = re.search(r"结算审核\s*包\s*(\d+)", scope_hint or "")

    def package_number_in_range(context: str, package_no: int) -> bool:
        ranges = re.findall(r"包\s*(\d+)\s*[-—至]\s*(?:包\s*)?(\d+)", context)
        if any(int(start) <= package_no <= int(end) for start, end in ranges):
            return True
        explicit = re.findall(r"结算审核\s*包\s*(\d+)(?!\d)", context)
        return str(package_no) in explicit

    recognized_tables = scoring_tables | price_tables
    for table_id, rows in grouped.items():
        if table_id not in recognized_tables:
            continue
        category = table_categories.get(table_id, "unclassified")
        table_context = table_contexts.get(table_id, "")
        if settlement_package:
            package_no = int(settlement_package.group(1))
            if category == "commercial" and "所有其他服务类分标" not in table_context:
                continue
            if category == "technical" and not (
                "结算审核" in table_context and package_number_in_range(table_context, package_no)
            ):
                continue
            if category == "price" and "投标报价评分标准" not in table_context:
                continue
        for block in rows[1:]:
            cells = [re.sub(r"\s+", " ", value).strip() for value in block.get("cells", [])]
            cells = [value for value in cells if value]
            text = " | ".join(cells) if cells else re.sub(r"\s+", " ", block.get("text", "")).strip()
            if len(text) < 8:
                continue
            if settlement_package and category == "commercial":
                if re.search(r"仅适用[^）)]*(?:施工|监理)(?:类)?分标", text):
                    continue
                if "仅适用" in text and "结算审核分标" not in text:
                    continue
                # A settlement-specific price-quality row replaces the generic row
                # for that same commercial scoring table.
                if "报价质量评价（40分）" in text:
                    continue
            # The third column names the score item and later columns carry its rubric.
            rubric = " ".join(cells[2:]) if len(cells) >= 3 else text
            if category == "unclassified":
                category, _ = _category(" ".join(cells), block.get("section_path", []))
            score_detail = " ".join(cells[3:]) if len(cells) >= 4 else text
            point_values = [float(match.group(1)) for match in POINTS.finditer(score_detail)]
            if not point_values and category != "price":
                continue
            score_label = cells[2] if len(cells) >= 3 else ""
            score_label_values = [float(match.group(1)) for match in POINTS.finditer(score_label)]
            leaf_score_values = (
                [float(match.group(1)) for match in POINTS.finditer(cells[-1])]
                if len(cells) >= 5 and cells[-1] != cells[-2]
                else []
            )
            max_score = None if category == "price" else (
                max(leaf_score_values) if leaf_score_values else (
                    max(score_label_values) if score_label_values else max(point_values)
                )
            )
            explicit_caps = [
                float(match.group(1))
                for match in re.finditer(r"(?:本项)?(?:最多|最高)(?:可)?得\s*(\d+(?:\.\d+)?)\s*分", score_detail)
            ]
            if explicit_caps and category != "price":
                max_score = max(explicit_caps)
            negative_range = re.search(r"[\(（]\s*(-\d+(?:\.\d+)?)\s*[-–至]\s*(-?\d+(?:\.\d+)?)(?:\s*分)?\s*[\)）]", score_label)
            if negative_range:
                max_score = max(float(negative_range.group(1)), float(negative_range.group(2)))
            elif re.search(r"\(\s*-\d+(?:\.\d+)?\s*分", score_label):
                max_score = 0.0
            criterion_label = cells[2] if len(cells) >= 3 else text
            if re.search(r"工作组织及人员配备|人员配备|工作组|项目组", criterion_label):
                subcategory = "team_staffing"
                related_subcategories = ["project_leader"] if re.search(r"项目负责人|项目经理", rubric) else []
            elif re.search(r"项目负责人|项目经理|技术负责人", criterion_label):
                subcategory = "project_leader"
                related_subcategories = []
            else:
                subcategory = _subcategory(criterion_label)
                related_subcategories = []
            conditions = []
            if subcategory == "team_staffing":
                numbered_conditions: dict[int, str] = {}
                for match in re.finditer(r"（(\d+)）(.*?)(?=（\d+）|$)", rubric, re.DOTALL):
                    number = int(match.group(1))
                    condition = f"（{number}）{match.group(2).strip()}"
                    if len(condition) > len(numbered_conditions.get(number, "")):
                        numbered_conditions[number] = condition
                conditions = [numbered_conditions[number] for number in sorted(numbered_conditions)]
                if score_label_values:
                    max_score = max(score_label_values)
            review_reason = "评分表行候选；列结构保留，须确认该评分表适用于所选包件并核对原文。"
            condition_caps = [
                float(match.group(1))
                for condition in conditions
                if (match := re.search(r"本项最高得\s*(\d+(?:\.\d+)?)\s*分", condition))
            ]
            if max_score is not None and condition_caps and sum(condition_caps) > max_score:
                review_reason += f" 子项封顶合计{sum(condition_caps):g}分，高于评分项标示的{max_score:g}分，需人工核实计分规则。"
            criteria.append(Criterion(
                criterion_id=str(uuid4()),
                category=category,
                criterion_label=criterion_label,
                subcategory=subcategory,
                related_subcategories=related_subcategories,
                source_text=text,
                score_text=rubric if category != "price" else None,
                formula_text=rubric if category == "price" else None,
                conditions=conditions,
                max_score=max_score,
                scope_label=table_context[-200:] or None,
                evidence=[_block_evidence(document_id, block)],
                extraction_state="candidate",
                review_state=ReviewState.PENDING,
                review_reason=review_reason,
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
    field_blocks = parsed["blocks"]
    if parsed.get("scope_hint") and parsed.get("archive_stats"):
        notice_blocks = [block for block in field_blocks
                         if "招标公告" in (block.get("source_file") or "")]
        if notice_blocks:
            field_blocks = notice_blocks
    fields = extract_fields(field_blocks, document_id)
    package_fields = _package_specific_fields(parsed["blocks"], document_id, parsed["scope_hint"])
    if package_fields:
        replacements = {item.field: item for item in package_fields}
        fields = [replacements.pop(item.field, item) for item in fields]
        fields.extend(replacements.values())
    criteria = extract_criteria(parsed["blocks"], document_id, parsed.get("scope_hint"))
    document = {
        "filename": filename,
        "sha256": parsed["sha256"],
        "document_id": document_id,
        "pages": parsed["pages"],
        "block_count": len(parsed["blocks"]),
        "chunk_count": len(parsed.get("chunks", [])),
        "scope_hint": parsed.get("scope_hint"),
        "source_documents": sorted({block.get("source_file") for block in parsed["blocks"] if block.get("source_file")}),
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
