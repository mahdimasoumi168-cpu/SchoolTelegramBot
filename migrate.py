from alembic import command
from alembic.config import Config


def upgrade_database():
    """Run all versioned database migrations."""
    config = Config("alembic.ini")
    command.upgrade(config, "head")
