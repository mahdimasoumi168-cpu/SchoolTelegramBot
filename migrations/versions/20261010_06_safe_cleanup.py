"""Mark new assignments and exams as temporary without deleting legacy records.

Existing rows are deliberately preserved (is_temporary=False). New ORM-created
records default to temporary and can be safely removed by nightly maintenance.
"""
from alembic import op

revision = "20261010_06_safe_cleanup"
down_revision = "20261006_05_tg_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE assignments ADD COLUMN IF NOT EXISTS "
        "is_temporary BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE exams ADD COLUMN IF NOT EXISTS "
        "is_temporary BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade():
    op.execute("ALTER TABLE assignments DROP COLUMN IF EXISTS is_temporary")
    op.execute("ALTER TABLE exams DROP COLUMN IF EXISTS is_temporary")
