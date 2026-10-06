"""Add per-student menu permissions.

The table is additive and preserves all existing student accounts.
"""
from alembic import op

revision = "20261006_04_student_permissions"
down_revision = "20261005_03_assigner_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS student_permission_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            lessons_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            assignments_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            schedule_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            exams_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            announcements_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            questions_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            account_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            notes_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            tomorrow_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            math_homework_enabled BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS student_permission_settings")
