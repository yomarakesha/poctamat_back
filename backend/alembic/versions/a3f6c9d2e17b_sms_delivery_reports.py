"""sms delivery reports

Revision ID: a3f6c9d2e17b
Revises: b0ebe32b50e8
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f6c9d2e17b'
down_revision: Union[str, Sequence[str], None] = 'b0ebe32b50e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('notifications', sa.Column('provider_message_id', sa.String(length=64), nullable=True))
    op.add_column('notifications', sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('notifications', sa.Column('delivery_status', sa.String(length=16), nullable=True))
    op.create_index(op.f('ix_notifications_provider_message_id'), 'notifications', ['provider_message_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_notifications_provider_message_id'), table_name='notifications')
    op.drop_column('notifications', 'delivery_status')
    op.drop_column('notifications', 'delivered_at')
    op.drop_column('notifications', 'provider_message_id')
