"""custody reason, disposal due date and handover document

Revision ID: b0ebe32b50e8
Revises: a8369e271825
Create Date: 2026-08-21 09:06:38.720410

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b0ebe32b50e8'
down_revision: Union[str, Sequence[str], None] = 'a8369e271825'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('custody_handovers',
                  sa.Column('document_ref', sa.String(length=128), nullable=True))
    # Server default so the column can be added to a table that already has
    # rows: acts filed before this migration were written with a description
    # only, and there is no reason to invent one for them.
    op.add_column('custody_records',
                  sa.Column('reason', sa.String(length=500), nullable=False,
                            server_default=''))
    op.add_column('custody_records',
                  sa.Column('disposal_due_at', sa.DateTime(timezone=True),
                            nullable=True))
    # Parcels already on the shelf get the same thirty-day clock, counted from
    # when they were removed — otherwise every one of them would read as due
    # immediately, which is exactly the mistake the waiting period prevents.
    op.execute(
        "UPDATE custody_records "
        "SET disposal_due_at = datetime(removed_at, '+30 days') "
        "WHERE disposal_due_at IS NULL"
    )
    # `status` widens from 16 to 24 characters for `returned_to_sender`. SQLite
    # does not enforce VARCHAR length and cannot ALTER a column type in place,
    # so the declared width is left to the model; a server that does enforce it
    # needs the batch operation below instead.
    if op.get_bind().dialect.name != 'sqlite':
        op.alter_column('custody_records', 'status',
                        existing_type=sa.VARCHAR(length=16),
                        type_=sa.String(length=24),
                        existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name != 'sqlite':
        op.alter_column('custody_records', 'status',
                        existing_type=sa.String(length=24),
                        type_=sa.VARCHAR(length=16),
                        existing_nullable=False)
    op.drop_column('custody_records', 'disposal_due_at')
    op.drop_column('custody_records', 'reason')
    op.drop_column('custody_handovers', 'document_ref')
