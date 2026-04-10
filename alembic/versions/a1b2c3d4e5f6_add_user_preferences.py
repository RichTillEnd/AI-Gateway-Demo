"""add_user_preferences

Revision ID: a1b2c3d4e5f6
Revises: 0c85c59fdcb8
Create Date: 2026-03-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '0c85c59fdcb8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('work_type', sa.String(50), nullable=True))
    op.add_column('users', sa.Column('user_instructions', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'user_instructions')
    op.drop_column('users', 'work_type')
