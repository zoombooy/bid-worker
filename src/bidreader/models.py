from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bidreader.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Tender(Base):
    __tablename__ = "tenders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(500), default="待识别项目")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    runs: Mapped[list["Run"]] = relationship(back_populates="tender", cascade="all, delete-orphan")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tender_id: Mapped[str] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(100))
    source_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(60), default="ingest")
    stage_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    tender: Mapped[Tender] = relationship(back_populates="runs")
    events: Mapped[list["RunEvent"]] = relationship(cascade="all, delete-orphan")
    analysis: Mapped["Analysis | None"] = relationship(cascade="all, delete-orphan", uselist=False)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_sequence", "run_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ReviewHistory(Base):
    __tablename__ = "review_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    criterion_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    reviewer: Mapped[str] = mapped_column(String(200), default="reviewer")
    action: Mapped[str] = mapped_column(String(40))
    before_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

