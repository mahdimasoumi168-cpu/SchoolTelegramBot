"""Baseline for the existing SchoolTelegramBot database.

The application historically created tables with SQLAlchemy before versioned
migrations were introduced. This revision records that baseline without
changing user data.
"""

from alembic import op

revision = "20261005_01_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
