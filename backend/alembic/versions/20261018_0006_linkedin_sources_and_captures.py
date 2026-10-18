"""linkedin source types and user captures

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18 22:43:25.971014+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("'official_website', 'press_release', 'annual_report', 'news', 'professional_profile', 'publication', "
        "'government', 'other'")
_NEW = ("'official_website', 'press_release', 'annual_report', 'news', 'professional_profile', 'publication', "
        "'government', 'linkedin_profile', 'linkedin_post', 'linkedin_activity', 'other'")


def upgrade() -> None:
    op.add_column('research_sources', sa.Column('captured_for_prospect_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_research_sources_captured_for_prospect_id'), 'research_sources',
                    ['captured_for_prospect_id'], unique=False)
    op.create_foreign_key(op.f('fk_research_sources_captured_for_prospect_id_prospects'), 'research_sources',
                          'prospects', ['captured_for_prospect_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint(op.f('ck_research_sources_sourcetype'), 'research_sources', type_='check')
    op.create_check_constraint(op.f('ck_research_sources_sourcetype'), 'research_sources', f"source_type IN ({_NEW})")


def downgrade() -> None:
    op.execute("DELETE FROM research_sources WHERE source_type LIKE 'linkedin_%'")
    op.drop_constraint(op.f('ck_research_sources_sourcetype'), 'research_sources', type_='check')
    op.create_check_constraint(op.f('ck_research_sources_sourcetype'), 'research_sources', f"source_type IN ({_OLD})")
    op.drop_constraint(op.f('fk_research_sources_captured_for_prospect_id_prospects'), 'research_sources',
                       type_='foreignkey')
    op.drop_index(op.f('ix_research_sources_captured_for_prospect_id'), table_name='research_sources')
    op.drop_column('research_sources', 'captured_for_prospect_id')
