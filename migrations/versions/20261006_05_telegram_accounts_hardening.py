"""Harden Telegram account links used by notification routing.

Adds the creation timestamp required by the current model and keeps the
multi-account lookup/indexes stable for existing production databases.
"""
from alembic import op

revision = "20261006_05_tg_accounts"
down_revision = "20261006_04_student_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE user_telegram_accounts "
        "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE user_telegram_accounts "
        "SET created_at = NOW() WHERE created_at IS NULL"
    )
    op.execute(
        "ALTER TABLE user_telegram_accounts "
        "ALTER COLUMN created_at SET DEFAULT NOW()"
    )
    op.execute(
        "ALTER TABLE user_telegram_accounts "
        "ALTER COLUMN created_at SET NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_telegram_accounts_user_created "
        "ON user_telegram_accounts (user_id, created_at DESC, id DESC)"
    )


def downgrade():
    op.execute(
        "DROP INDEX IF EXISTS ix_user_telegram_accounts_user_created"
    )
    op.execute(
        "ALTER TABLE user_telegram_accounts "
        "DROP COLUMN IF EXISTS created_at"
    )
