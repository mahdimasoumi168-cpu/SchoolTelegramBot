"""Harden the historical production schema.

This migration replaces the old startup-time ALTER TABLE block. Statements
are idempotent so the migration is safe for existing and fresh databases.
"""

from alembic import op

revision = "20261005_02_schema_hardening"
down_revision = "20261005_01_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_telegram_id_key")
    op.execute("DROP INDEX IF EXISTS users_telegram_id_key")

    op.execute("ALTER TABLE schedules ALTER COLUMN period TYPE TEXT USING period::text")
    op.execute("ALTER TABLE schedules ALTER COLUMN weekday TYPE TEXT USING weekday::text")

    op.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    op.execute("ALTER TABLE announcements ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    op.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS student_notified BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("UPDATE questions SET student_notified = FALSE WHERE status = 'OPEN'")
    op.execute("ALTER TABLE questions ALTER COLUMN student_notified SET DEFAULT FALSE")

    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_username VARCHAR(100)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(300)")
    op.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS school_code VARCHAR(80) DEFAULT ''")
    op.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS login_name VARCHAR(150) DEFAULT ''")

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_login_username_unique "
        "ON users (login_username) WHERE login_username IS NOT NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_students_school_code ON students (school_code)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_students_login_name ON students (login_name)")

    op.execute(
        "ALTER TABLE user_telegram_accounts "
        "DROP CONSTRAINT IF EXISTS user_telegram_accounts_telegram_id_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_user_telegram_accounts_telegram_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_telegram_accounts_telegram_id "
        "ON user_telegram_accounts (telegram_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS active_telegram_sessions (
            telegram_id BIGINT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "ALTER TABLE active_telegram_sessions "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_active_telegram_sessions_user_id "
        "ON active_telegram_sessions (user_id)"
    )


def downgrade():
    # This revision consolidates legacy production changes and avoids
    # destructive rollback of production user data.
    pass
