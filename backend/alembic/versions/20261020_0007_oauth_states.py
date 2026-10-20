"""oauth states for mailbox connection

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20 18:25:58.425885+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('oauth_states',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('state_hash', sa.String(length=64), nullable=False),
    sa.Column('encrypted_code_verifier', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("provider IN ('gmail', 'microsoft')", name=op.f('ck_oauth_states_emailprovider')),
    sa.ForeignKeyConstraint(['session_id'], ['user_sessions.id'], name=op.f('fk_oauth_states_session_id_user_sessions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_oauth_states_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_oauth_states_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oauth_states')),
    sa.UniqueConstraint('state_hash', name=op.f('uq_oauth_states_state_hash'))
    )
    op.create_index(op.f('ix_oauth_states_session_id'), 'oauth_states', ['session_id'], unique=False)
    op.create_index(op.f('ix_oauth_states_workspace_id'), 'oauth_states', ['workspace_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_oauth_states_workspace_id'), table_name='oauth_states')
    op.drop_index(op.f('ix_oauth_states_session_id'), table_name='oauth_states')
    op.drop_table('oauth_states')
