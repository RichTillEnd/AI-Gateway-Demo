"""expand_reset_token_to_100

Revision ID: 43e66ce98b19
Revises: 0dde76d6eada
Create Date: 2026-04-01 16:06:21.188031

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '43e66ce98b19'
down_revision: Union[str, Sequence[str], None] = '0dde76d6eada'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Expand reset_token column from VARCHAR(10) to VARCHAR(100) to support token_urlsafe(32) tokens."""
    op.alter_column(
        "users",
        "reset_token",
        existing_type=sa.String(10),
        type_=sa.String(100),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Revert reset_token column back to VARCHAR(10)."""
    op.alter_column(
        "users",
        "reset_token",
        existing_type=sa.String(100),
        type_=sa.String(10),
        existing_nullable=True,
    )
