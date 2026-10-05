"""Add per-determiner menu button permissions.

This migration is additive and preserves all existing accounts and data.
"""
from alembic import op

revision = "20261005_03_assigner_permissions"
down_revision = "20261005_02_schema_hardening"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assigner_permission_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            students_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            lessons_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            assignments_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            announcements_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            schedule_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            exams_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            notes_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            questions_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            tomorrow_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            photo_homework_enabled BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS assigner_permission_settings")
