"""Baseline: the database exactly as increment 6 left it.

Before increment 7, tables were created by database.create_all() at start-up.
This migration describes those same five tables so that a brand-new database
gets them from the migration history.

Every table is created only if it is missing. On an existing database (local
SQLite or the live PostgreSQL) all five tables already exist, so this step
changes nothing and only marks the database as being at this revision.
Constraint names are deliberately left to the database's defaults, so they
match the ones create_all() produced on the live database.

Revision ID: 0001_baseline
Revises: (none)
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def existing_tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    tables_already_present = existing_tables()

    if "user" not in tables_already_present:
        op.create_table(
            "user",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("full_name", sa.String(length=120), nullable=False),
            sa.Column("email", sa.String(length=120), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("reset_token", sa.String(length=64), nullable=True),
            sa.Column("reset_token_expiry", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email"),
        )

    if "document" not in tables_already_present:
        op.create_table(
            "document",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("file_name", sa.String(length=255), nullable=False),
            sa.Column("stored_name", sa.String(length=255), nullable=False),
            sa.Column("file_format", sa.String(length=10), nullable=True),
            sa.Column("upload_date", sa.DateTime(), nullable=True),
            sa.Column("summary_text", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if "extracted_clause" not in tables_already_present:
        op.create_table(
            "extracted_clause",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("clause_category", sa.String(length=80), nullable=True),
            sa.Column("extracted_text", sa.Text(), nullable=True),
            sa.Column("risk_severity", sa.String(length=20), nullable=True),
            sa.Column("explanation", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if "document_analysis" not in tables_already_present:
        op.create_table(
            "document_analysis",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("summary_depth", sa.String(length=40), nullable=True),
            sa.Column("extraction_mode", sa.String(length=40), nullable=True),
            sa.Column("risk_level", sa.String(length=40), nullable=True),
            sa.Column("context_hint", sa.String(length=40), nullable=True),
            sa.Column("summary_method", sa.String(length=120), nullable=True),
            sa.Column("truncated_for_ai", sa.Boolean(), nullable=True),
            sa.Column("processing_seconds", sa.Float(), nullable=True),
            sa.Column("extracted_character_count", sa.Integer(), nullable=True),
            sa.Column("extracted_text", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_id"),
        )

    if "user_preference" not in tables_already_present:
        op.create_table(
            "user_preference",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("theme", sa.String(length=10), nullable=False),
            sa.Column("default_summary_depth", sa.String(length=40), nullable=False),
            sa.Column("default_extraction_mode", sa.String(length=40), nullable=False),
            sa.Column("default_risk_level", sa.String(length=40), nullable=False),
            sa.Column("default_context_hint", sa.String(length=40), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id"),
        )


def downgrade():
    # Removing the baseline would delete every account and document. Refuse,
    # so a mistyped "flask db downgrade base" cannot wipe the database.
    raise RuntimeError("The baseline migration cannot be undone: it would delete all data.")
