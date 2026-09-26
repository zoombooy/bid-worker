from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ReviewState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class Evidence(BaseModel):
    document_id: str
    source_file: str | None = None
    page_no: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    block_id: str
    quote: str
    bbox: list[float] | None = None
    source_kind: str = "text"

    @model_validator(mode="after")
    def valid_bbox(self):
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox must contain x0, y0, x1, y1")
        return self


class ParsedBlock(BaseModel):
    block_id: str
    kind: str
    text: str
    page_no: int | None = None
    bbox: list[float] | None = None
    section_path: list[str] = Field(default_factory=list)
    source_index: int
    table_id: str | None = None
    row_index: int | None = None
    cell_index: int | None = None
    cells: list[str] = Field(default_factory=list)
    source_file: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None


class ProjectField(BaseModel):
    field: str
    raw_value: str | None = None
    normalized_value: str | None = None
    state: str = "not_found"
    confidence: float | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    review_state: ReviewState = ReviewState.PENDING
    review_reason: str | None = None


class Criterion(BaseModel):
    criterion_id: str
    category: str
    criterion_label: str | None = None
    parent_label: str | None = None
    subcategory: str | None = None
    related_subcategories: list[str] = Field(default_factory=list)
    source_text: str
    score_text: str | None = None
    max_score: float | None = None
    tiers: list[dict[str, Any]] = Field(default_factory=list)
    evidence_required: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    formula_text: str | None = None
    scope_label: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    extraction_state: str = "candidate"
    review_state: ReviewState = ReviewState.PENDING
    review_reason: str | None = None


class AnalysisResult(BaseModel):
    run_id: str
    document: dict[str, Any]
    project_fields: list[ProjectField] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    ledger: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parser_versions: dict[str, str] = Field(default_factory=dict)


class ReviewPatch(BaseModel):
    action: ReviewState
    source_text: str | None = None
    score_text: str | None = None
    max_score: float | None = None
    category: Literal["technical", "commercial", "price", "unclassified"] | None = None
    subcategory: str | None = None
    evidence: list[Evidence] | None = None
    reviewer: str = "reviewer"
    note: str = ""


class FieldReviewPatch(BaseModel):
    action: ReviewState
    raw_value: str | None = None
    evidence: list[Evidence] | None = None
    reviewer: str = "reviewer"
    note: str = ""


class RunConfirmation(BaseModel):
    reviewer: str = "reviewer"
    note: str = "已核对全文、处理账本及所有候选结果。"
    confirm_no_scoring_criteria: bool = False
