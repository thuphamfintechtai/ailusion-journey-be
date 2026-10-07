"""add users created_at index

Revision ID: b7e2c41f9d10
Revises: acda7b383a27
Create Date: 2026-10-07 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2c41f9d10"
down_revision: str | Sequence[str] | None = "acda7b383a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # CONCURRENTLY: don't lock writes on users while the index builds.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_users_created_at",
            "users",
            ["created_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_users_created_at",
            table_name="users",
            postgresql_concurrently=True,
            if_exists=True,
        )
