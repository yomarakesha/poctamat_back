"""cell types in millimetres

Revision ID: 2cc2efde0f58
Revises: 20c05556f5d8
Create Date: 2026-08-18 14:32:52.102746

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2cc2efde0f58'
down_revision: Union[str, Sequence[str], None] = '20c05556f5d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable first, then filled, then made NOT NULL: the table has rows and a
    # NOT NULL column cannot be added to one without a default.
    op.add_column('cell_types', sa.Column('width_mm', sa.Integer(), nullable=True))
    op.add_column('cell_types', sa.Column('height_mm', sa.Integer(), nullable=True))
    op.add_column('cell_types', sa.Column('depth_mm', sa.Integer(), nullable=True))
    op.add_column('cell_types', sa.Column('blocked_at', sa.DateTime(timezone=True),
                                          nullable=True))
    # The same drawer in millimetres is ten times the number.
    op.execute("UPDATE cell_types SET width_mm = width_cm * 10, "
               "height_mm = height_cm * 10, depth_mm = depth_cm * 10")
    with op.batch_alter_table('cell_types') as batch:
        batch.alter_column('width_mm', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('height_mm', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('depth_mm', existing_type=sa.Integer(), nullable=False)
    op.drop_column('cell_types', 'width_cm')
    op.drop_column('cell_types', 'height_cm')
    op.drop_column('cell_types', 'depth_cm')
    op.execute("UPDATE cell_types SET blocked_at = CURRENT_TIMESTAMP "
               "WHERE is_blocked = 1")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('cell_types', sa.Column('width_cm', sa.Integer(), nullable=True))
    op.add_column('cell_types', sa.Column('height_cm', sa.Integer(), nullable=True))
    op.add_column('cell_types', sa.Column('depth_cm', sa.Integer(), nullable=True))
    # Integer division: a size that was not a whole number of centimetres cannot
    # come back as one, and rounding down is the honest answer.
    op.execute("UPDATE cell_types SET width_cm = width_mm / 10, "
               "height_cm = height_mm / 10, depth_cm = depth_mm / 10")
    with op.batch_alter_table('cell_types') as batch:
        batch.alter_column('width_cm', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('height_cm', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('depth_cm', existing_type=sa.Integer(), nullable=False)
    op.drop_column('cell_types', 'blocked_at')
    op.drop_column('cell_types', 'depth_mm')
    op.drop_column('cell_types', 'height_mm')
    op.drop_column('cell_types', 'width_mm')
