"""provider_message_id unique

Revision ID: f4c8b21ade90
Revises: a3f6c9d2e17b
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f4c8b21ade90'
down_revision: Union[str, Sequence[str], None] = 'a3f6c9d2e17b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('notifications') as batch:
        batch.drop_index(op.f('ix_notifications_provider_message_id'))
        batch.create_unique_constraint(
            op.f('uq_notifications_provider_message_id'), ['provider_message_id']
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('notifications') as batch:
        batch.drop_constraint(
            op.f('uq_notifications_provider_message_id'), type_='unique'
        )
        batch.create_index(
            op.f('ix_notifications_provider_message_id'), ['provider_message_id']
        )
