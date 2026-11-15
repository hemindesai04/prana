from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all Prana relational models."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    ingest_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sex: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sleep_target_hours: Mapped[float] = mapped_column(
        Double, nullable=False, server_default=text("7.5")
    )
    latest_vo2_max: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    sleep_debt_hours: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    notification_config: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RawIngestion(Base):
    __tablename__ = "raw_ingestions"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "payload_sha256", name="uq_raw_user_source_sha256"),
        Index("idx_raw_ingestions_user_source_time", "user_id", "source", desc("received_at")),
        Index("idx_raw_ingestions_status", "status", desc("received_at")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Biomarker(Base):
    __tablename__ = "biomarkers"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source",
            "device",
            "metric_name",
            "recorded_at",
            name="uq_biomarkers_idempotency",
        ),
        Index("idx_biomarkers_user_metric_time", "user_id", "metric_name", desc("recorded_at")),
        Index("idx_biomarkers_user_canonical_time", "user_id", "canonical_metric", desc("recorded_at")),
        Index("idx_biomarkers_user_source_time", "user_id", "source", desc("recorded_at")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    device: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_metric: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    value_min: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    value_max: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source",
            "device",
            "workout_type",
            "started_at",
            name="uq_workouts_idempotency",
        ),
        Index("idx_workouts_user_time", "user_id", desc("started_at")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    device: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    workout_type: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    calories: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IngestionIssue(Base):
    __tablename__ = "ingestion_issues"
    __table_args__ = (
        Index("idx_ingestion_issues_open", "resolved_at", "severity", desc("created_at")),
        Index("idx_ingestion_issues_raw", "raw_ingestion_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    raw_ingestion_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("raw_ingestions.id", ondelete="CASCADE"), nullable=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # "sos" | "warning"
    issue_type: Mapped[str] = mapped_column(Text, nullable=False)
    metric_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BackupRun(Base):
    __tablename__ = "backup_runs"
    __table_args__ = (Index("idx_backup_runs_started", desc("started_at")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # "success" | "failed"
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "day",
            "canonical_metric",
            "source",
            "device",
            name="uq_daily_metrics_key",
        ),
        Index("idx_daily_metrics_user_metric_day", "user_id", "canonical_metric", desc("day")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    canonical_metric: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    device: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    value: Mapped[float] = mapped_column(Double, nullable=False)
    value_min: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    value_max: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    baseline_mean: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    baseline_sd: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    z_score: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Insight(Base):
    __tablename__ = "insights"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "rule_id", name="uq_insights_user_day_rule"),
        Index("idx_insights_user_day", "user_id", desc("day")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # "info" | "watch" | "alert"
    title: Mapped[str] = mapped_column(Text, nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Briefing(Base):
    __tablename__ = "briefings"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "period", name="uq_briefings_user_day_period"),
        Index("idx_briefings_user_day", "user_id", desc("day"), "period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    period: Mapped[str] = mapped_column(Text, nullable=False)  # "morning" | "evening"
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    action_items: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    metrics_snapshot: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    model_used: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
