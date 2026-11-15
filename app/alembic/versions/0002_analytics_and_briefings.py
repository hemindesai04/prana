"""analytics, user profile metrics, insights, and briefings

Revision ID: 0002_analytics
Revises: 0001_initial
Create Date: 2026-11-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_analytics"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("sex", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("sleep_target_hours", sa.Double(), nullable=False, server_default=sa.text("7.5")),
    )
    op.add_column("users", sa.Column("latest_vo2_max", sa.Double(), nullable=True))
    op.add_column("users", sa.Column("sleep_debt_hours", sa.Double(), nullable=True))

    op.execute(
        """
        UPDATE users
        SET date_of_birth = '1992-06-04',
            sex = 'male',
            sleep_target_hours = 7.5
        WHERE slug = 'hemin'
        """
    )

    op.create_table(
        "daily_metrics",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("canonical_metric", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("device", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("value_min", sa.Double(), nullable=True),
        sa.Column("value_max", sa.Double(), nullable=True),
        sa.Column("sample_count", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("baseline_mean", sa.Double(), nullable=True),
        sa.Column("baseline_sd", sa.Double(), nullable=True),
        sa.Column("z_score", sa.Double(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id", "day", "canonical_metric", "source", "device", name="uq_daily_metrics_key"
        ),
    )
    op.create_index(
        "idx_daily_metrics_user_metric_day",
        "daily_metrics",
        ["user_id", "canonical_metric", sa.text("day DESC")],
    )

    op.create_table(
        "insights",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("finding", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
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
        sa.UniqueConstraint("user_id", "day", "rule_id", name="uq_insights_user_day_rule"),
    )
    op.create_index(
        "idx_insights_user_day",
        "insights",
        ["user_id", sa.text("day DESC")],
    )

    op.create_table(
        "briefings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary_markdown", sa.Text(), nullable=False),
        sa.Column(
            "action_items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "metrics_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("model_used", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "day", "period", name="uq_briefings_user_day_period"),
    )
    op.create_index(
        "idx_briefings_user_day",
        "briefings",
        ["user_id", sa.text("day DESC"), "period"],
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'prana_ro') THEN
                GRANT SELECT ON ALL TABLES IN SCHEMA public TO prana_ro;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("idx_briefings_user_day", table_name="briefings")
    op.drop_table("briefings")
    op.drop_index("idx_insights_user_day", table_name="insights")
    op.drop_table("insights")
    op.drop_index("idx_daily_metrics_user_metric_day", table_name="daily_metrics")
    op.drop_table("daily_metrics")
    op.drop_column("users", "sleep_debt_hours")
    op.drop_column("users", "latest_vo2_max")
    op.drop_column("users", "sleep_target_hours")
    op.drop_column("users", "sex")
    op.drop_column("users", "date_of_birth")
