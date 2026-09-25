"""Deterministic evaluation for manually annotated tender gold samples."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

REQUIRED_COVERAGE = {
    "project_fields",
    "technical_criteria",
    "commercial_criteria",
    "price_criteria",
}


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", text)


def _bigram_dice(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return 1.0 if min(len(left), len(right)) >= 12 else min(len(left), len(right)) / max(len(left), len(right))
    if len(left) == 1 or len(right) == 1:
        return 1.0 if left == right else 0.0
    left_pairs = [left[index:index + 2] for index in range(len(left) - 1)]
    right_pairs = [right[index:index + 2] for index in range(len(right) - 1)]
    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for pair in left_pairs:
        left_counts[pair] = left_counts.get(pair, 0) + 1
    for pair in right_pairs:
        right_counts[pair] = right_counts.get(pair, 0) + 1
    overlap = sum(min(count, right_counts.get(pair, 0)) for pair, count in left_counts.items())
    return 2 * overlap / (len(left_pairs) + len(right_pairs))


def _candidate_quotes(criterion: dict[str, Any]) -> list[str]:
    quotes = [criterion.get("source_text", "")]
    quotes.extend(item.get("quote", "") for item in criterion.get("evidence", []))
    return [normalize_text(quote) for quote in quotes if quote]


def _quote_similarity(gold_quote: str, candidate: dict[str, Any]) -> float:
    gold = normalize_text(gold_quote)
    return max((_bigram_dice(gold, quote) for quote in _candidate_quotes(candidate)), default=0.0)


def _candidate_pages(criterion: dict[str, Any]) -> set[int]:
    return {item["page_no"] for item in criterion.get("evidence", []) if item.get("page_no") is not None}


def _bbox_iou(left: Iterable[float], right: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = (float(value) for value in left)
    bx0, by0, bx1, by1 = (float(value) for value in right)
    intersection = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))
    left_area = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    right_area = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _rate(correct: int, total: int) -> float | None:
    return round(correct / total, 4) if total else None


def _f1(precision: float, recall: float) -> float:
    return round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0


def _validate_gold(gold: dict[str, Any]) -> None:
    if gold.get("schema_version") != "1.0":
        raise ValueError("gold.schema_version 必须为 1.0。")
    if gold.get("annotation_complete") is not True:
        raise ValueError("黄金标注尚未标记 annotation_complete=true。")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", gold.get("document", {}).get("sha256", "")):
        raise ValueError("gold.document.sha256 必须是被标注原件的 SHA-256。")
    coverage = set(gold.get("coverage_reviewed", []))
    if not REQUIRED_COVERAGE.issubset(coverage):
        raise ValueError(f"coverage_reviewed 必须包含：{', '.join(sorted(REQUIRED_COVERAGE))}。")
    if not isinstance(gold.get("project_fields"), dict):
        raise TypeError("gold.project_fields 必须是对象；null 表示人工确认原件中未发现该字段。")
    if not isinstance(gold.get("criteria"), list):
        raise TypeError("gold.criteria 必须是完整人工审核后的评分项数组。")


def evaluate(gold: dict[str, Any], prediction: dict[str, Any], *, match_threshold: float = 0.75) -> dict[str, Any]:
    """Compare one analysis result against full-project-isolated human gold labels."""
    _validate_gold(gold)
    analysis = prediction.get("analysis", prediction)
    if not isinstance(analysis, dict):
        raise TypeError("prediction 必须是 analysis 对象或 API 返回的 {analysis: ...} 对象。")
    predicted_sha = analysis.get("document", {}).get("sha256")
    if predicted_sha != gold["document"]["sha256"]:
        raise ValueError("黄金标注与预测结果的原件 SHA-256 不一致，拒绝跨版本计算指标。")

    predicted_fields = {item.get("field"): item for item in analysis.get("project_fields", [])}
    field_details = []
    for name, expected in gold["project_fields"].items():
        expected_value = expected.get("value") if isinstance(expected, dict) else expected
        actual = predicted_fields.get(name, {})
        actual_value = actual.get("raw_value")
        exact = normalize_text(expected_value) == normalize_text(actual_value)
        field_details.append({"field": name, "expected": expected_value, "actual": actual_value, "exact": exact})

    gold_criteria = gold["criteria"]
    predicted_criteria = analysis.get("criteria", [])
    pair_candidates = []
    for gold_index, expected in enumerate(gold_criteria):
        expected_page = expected.get("page_no")
        for predicted_index, actual in enumerate(predicted_criteria):
            pages = _candidate_pages(actual)
            if expected_page is not None and expected_page not in pages:
                continue
            similarity = _quote_similarity(expected.get("source_quote", ""), actual)
            if similarity >= match_threshold:
                pair_candidates.append((similarity, gold_index, predicted_index))
    pair_candidates.sort(reverse=True)
    matches: dict[int, tuple[int, float]] = {}
    used_predictions: set[int] = set()
    for similarity, gold_index, predicted_index in pair_candidates:
        if gold_index not in matches and predicted_index not in used_predictions:
            matches[gold_index] = (predicted_index, similarity)
            used_predictions.add(predicted_index)

    matched_details = []
    category_total = category_correct = score_total = score_correct = 0
    evidence_quote_correct = evidence_page_correct = evidence_total = 0
    bbox_correct = bbox_total = 0
    for gold_index, (predicted_index, similarity) in matches.items():
        expected = gold_criteria[gold_index]
        actual = predicted_criteria[predicted_index]
        category_total += 1
        category_correct += actual.get("category") == expected.get("category")
        expected_score = expected.get("max_score")
        if expected_score is not None:
            score_total += 1
            score_correct += actual.get("max_score") == expected_score
        expected_page = expected.get("page_no")
        expected_quote = normalize_text(expected.get("source_quote", ""))
        evidence = actual.get("evidence", [])
        evidence_total += 1
        page_quote_match = [
            item for item in evidence
            if (expected_page is None or item.get("page_no") == expected_page)
            and expected_quote in normalize_text(item.get("quote", ""))
        ]
        evidence_quote_correct += bool(page_quote_match)
        evidence_page_correct += expected_page is None or any(item.get("page_no") == expected_page for item in evidence)
        expected_bbox = expected.get("bbox")
        if expected_bbox is not None:
            bbox_total += 1
            bbox_correct += any(item.get("bbox") and _bbox_iou(expected_bbox, item["bbox"]) >= 0.5 for item in page_quote_match)
        matched_details.append({
            "gold_index": gold_index,
            "prediction_criterion_id": actual.get("criterion_id"),
            "quote_similarity": round(similarity, 4),
            "category_correct": actual.get("category") == expected.get("category"),
            "score_correct": expected_score is None or actual.get("max_score") == expected_score,
            "evidence_quote_and_page_correct": bool(page_quote_match),
        })

    true_positive = len(matches)
    precision = true_positive / len(predicted_criteria) if predicted_criteria else (1.0 if not gold_criteria else 0.0)
    recall = true_positive / len(gold_criteria) if gold_criteria else (1.0 if not predicted_criteria else 0.0)
    metric_counts = {
        "field_total": len(field_details),
        "field_correct": sum(item["exact"] for item in field_details),
        "category_total": category_total,
        "category_correct": category_correct,
        "score_total": score_total,
        "score_correct": score_correct,
        "evidence_total": evidence_total,
        "evidence_quote_correct": evidence_quote_correct,
        "evidence_page_correct": evidence_page_correct,
        "bbox_total": bbox_total,
        "bbox_correct": bbox_correct,
    }
    return {
        "schema_version": "1.0",
        "document_sha256": predicted_sha,
        "counts": {"gold_criteria": len(gold_criteria), "predicted_criteria": len(predicted_criteria),
                   "matched_criteria": true_positive, "unmatched_gold": len(gold_criteria) - true_positive,
                   "unmatched_predictions": len(predicted_criteria) - true_positive, **metric_counts},
        "metrics": {
            "project_field_exact_accuracy": _rate(sum(item["exact"] for item in field_details), len(field_details)),
            "criterion_precision": round(precision, 4),
            "criterion_recall": round(recall, 4),
            "criterion_f1": _f1(precision, recall),
            "criterion_category_accuracy": _rate(category_correct, category_total),
            "criterion_max_score_accuracy": _rate(score_correct, score_total),
            "evidence_quote_and_page_accuracy": _rate(evidence_quote_correct, evidence_total),
            "evidence_page_accuracy": _rate(evidence_page_correct, evidence_total),
            "bbox_iou_at_least_0_5_accuracy": _rate(bbox_correct, bbox_total),
        },
        "field_details": field_details,
        "criterion_matches": matched_details,
        "unmatched_gold_criteria": [index for index in range(len(gold_criteria)) if index not in matches],
        "unmatched_prediction_ids": [
            item.get("criterion_id") for index, item in enumerate(predicted_criteria) if index not in used_predictions
        ],
        "matching": {"method": "same annotated page when provided; character-bigram Dice", "threshold": match_threshold},
        "limitations": [
            "分值准确率只在匹配到的评分项上统计。",
            "字段准确率只覆盖黄金标注中明确列出的字段。",
            "BBox 指标只覆盖人工标注 bbox 的黄金评分项，坐标须使用 PDF 页面点单位。",
            "多个页面的综合结果必须按项目划分训练/调试与盲测集后分别统计。",
        ],
    }


def _aggregate_metrics(reports: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for report in reports:
        for name, value in report["counts"].items():
            counts[name] = counts.get(name, 0) + value
    gold_count = counts.get("gold_criteria", 0)
    prediction_count = counts.get("predicted_criteria", 0)
    matches = counts.get("matched_criteria", 0)
    precision = matches / prediction_count if prediction_count else (1.0 if not gold_count else 0.0)
    recall = matches / gold_count if gold_count else (1.0 if not prediction_count else 0.0)
    return {
        "samples": len(reports),
        "counts": counts,
        "metrics": {
            "project_field_exact_accuracy": _rate(counts.get("field_correct", 0), counts.get("field_total", 0)),
            "criterion_precision": round(precision, 4),
            "criterion_recall": round(recall, 4),
            "criterion_f1": _f1(precision, recall),
            "criterion_category_accuracy": _rate(counts.get("category_correct", 0), counts.get("category_total", 0)),
            "criterion_max_score_accuracy": _rate(counts.get("score_correct", 0), counts.get("score_total", 0)),
            "evidence_quote_and_page_accuracy": _rate(counts.get("evidence_quote_correct", 0), counts.get("evidence_total", 0)),
            "evidence_page_accuracy": _rate(counts.get("evidence_page_correct", 0), counts.get("evidence_total", 0)),
            "bbox_iou_at_least_0_5_accuracy": _rate(counts.get("bbox_correct", 0), counts.get("bbox_total", 0)),
        },
    }


def aggregate_corpus(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-document reports and fail if a project leaks across data splits."""
    project_splits: dict[str, str] = {}
    for sample in samples:
        project_id = sample.get("project_id")
        split = sample.get("split")
        if not project_id or split not in {"development", "blind_test"}:
            raise ValueError("每个样本必须有 project_id，并指定 development 或 blind_test split。")
        previous = project_splits.setdefault(project_id, split)
        if previous != split:
            raise ValueError(f"项目 {project_id} 同时进入多个数据集划分，存在项目级泄漏。")
    def grouped(key: str) -> dict[str, Any]:
        result = {}
        for sample in samples:
            label = sample.get(key, "unspecified")
            result.setdefault(label, []).append(sample["report"])
        return {label: _aggregate_metrics(reports) for label, reports in sorted(result.items())}
    reports = [sample["report"] for sample in samples]
    return {
        "schema_version": "1.0",
        "project_split_count": len(project_splits),
        "overall_micro": _aggregate_metrics(reports),
        "by_split": grouped("split"),
        "by_format": grouped("format"),
        "per_sample": [{"project_id": sample["project_id"], "split": sample["split"],
                        "format": sample.get("format", "unspecified"), "report": sample["report"]}
                       for sample in samples],
    }
