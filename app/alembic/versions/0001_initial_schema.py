"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("ingest_token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column(
            "notification_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "raw_ingestions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("user_id", "source", "payload_sha256", name="uq_raw_user_source_sha256"),
    )
    op.create_index(
        "idx_raw_ingestions_user_source_time",
        "raw_ingestions",
        ["user_id", "source", sa.text("received_at DESC")],
    )
    op.create_index(
        "idx_raw_ingestions_status",
        "raw_ingestions",
        ["status", sa.text("received_at DESC")],
    )

    op.create_table(
        "biomarkers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("device", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("canonical_metric", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Double(), nullable=True),
        sa.Column("value_min", sa.Double(), nullable=True),
        sa.Column("value_max", sa.Double(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "source",
            "device",
            "metric_name",
            "recorded_at",
            name="uq_biomarkers_idempotency",
        ),
    )
    op.create_index(
        "idx_biomarkers_user_metric_time",
        "biomarkers",
        ["user_id", "metric_name", sa.text("recorded_at DESC")],
    )
    op.create_index(
        "idx_biomarkers_user_canonical_time",
        "biomarkers",
        ["user_id", "canonical_metric", sa.text("recorded_at DESC")],
    )
    op.create_index(
        "idx_biomarkers_user_source_time",
        "biomarkers",
        ["user_id", "source", sa.text("recorded_at DESC")],
    )

    op.create_table(
        "workouts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("device", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("workout_type", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Double(), nullable=True),
        sa.Column("calories", sa.Double(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "source",
            "device",
            "workout_type",
            "started_at",
            name="uq_workouts_idempotency",
        ),
    )
    op.create_index(
        "idx_workouts_user_time",
        "workouts",
        ["user_id", sa.text("started_at DESC")],
    )

    op.create_table(
        "ingestion_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "raw_ingestion_id",
            sa.BigInteger(),
            sa.ForeignKey("raw_ingestions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("issue_type", sa.Text(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_ingestion_issues_open",
        "ingestion_issues",
        ["resolved_at", "severity", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_ingestion_issues_raw",
        "ingestion_issues",
        ["raw_ingestion_id"],
    )

    op.create_table(
        "backup_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("idx_backup_runs_started", "backup_runs", [sa.text("started_at DESC")])


def downgrade() -> None:
    op.drop_table("backup_runs")
    op.drop_table("ingestion_issues")
    op.drop_table("workouts")
    op.drop_table("biomarkers")
    op.drop_table("raw_ingestions")
    op.drop_table("users")
