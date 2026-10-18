"""Database-level guarantees. These constraints are the last line of defense behind service logic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.transitions import StaleTransition, transition_row
from app.domain.states import (
    SCHEDULED_ACTION,
    ContentPolicy,
    DraftStatus,
    DraftType,
    EmailProvider,
    ResearchSubject,
    ScheduledActionState,
    ScheduledActionType,
    SuppressionReason,
    SuppressionScope,
    SuppressionSource,
)
from app.models import (
    Campaign,
    Company,
    EmailDraft,
    Prospect,
    ProviderEvent,
    ResearchRun,
    ScheduledAction,
    Sequence,
    SequenceStep,
    Suppression,
    Workspace,
)
from tests.conftest import alembic_config

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)


@pytest.fixture
def ws(db: Session) -> Workspace:
    w = Workspace(name="Test")
    db.add(w)
    db.flush()
    return w


def _expect_integrity_error(db: Session, obj: object, constraint: str) -> None:
    db.add(obj)
    with pytest.raises(IntegrityError) as exc, db.begin_nested():
        db.flush()
    assert constraint in str(exc.value)


def test_schema_is_at_head_with_no_drift(engine: Engine) -> None:
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        head = ScriptDirectory.from_config(alembic_config(conn)).get_current_head()
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert current == head
    assert diff == [], f"models and migrations disagree: {diff}"


def test_prospect_identity_is_unique_per_workspace_email(db: Session, ws: Workspace) -> None:
    db.add(Prospect(workspace_id=ws.id, email="a@x.com", email_normalized="a@x.com"))
    db.flush()
    _expect_integrity_error(db, Prospect(workspace_id=ws.id, email="A@x.com", email_normalized="a@x.com"),
                            "uq_prospects_workspace_email")


def test_prospects_without_email_and_same_name_are_not_merged(db: Session, ws: Workspace) -> None:
    db.add_all([Prospect(workspace_id=ws.id, full_name="Jane Doe"), Prospect(workspace_id=ws.id,
                                                                              full_name="Jane Doe")])
    db.flush()  # no unique violation: same name is not the same person


def test_same_email_allowed_in_different_workspaces(db: Session, ws: Workspace) -> None:
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    db.add_all([Prospect(workspace_id=ws.id, email_normalized="a@x.com"),
                Prospect(workspace_id=other.id, email_normalized="a@x.com")])
    db.flush()


def test_company_domain_unique_per_workspace(db: Session, ws: Workspace) -> None:
    db.add(Company(workspace_id=ws.id, name="Acme", normalized_domain="acme.com"))
    db.flush()
    _expect_integrity_error(db, Company(workspace_id=ws.id, name="ACME Inc", normalized_domain="acme.com"),
                            "uq_companies_workspace_domain")


def test_enum_check_constraint_rejects_unknown_values(db: Session, ws: Workspace) -> None:
    with pytest.raises(IntegrityError, match="ck_campaigns_campaignstatus"), db.begin_nested():
        db.execute(text("INSERT INTO campaigns (workspace_id, name, timezone, sending_policy, status, motion, brief, "
                        "brief_version) VALUES (:w, 'c', 'UTC', '{}', 'yolo', 'product_sale', '{}', 1)"),
                   {"w": ws.id})


def test_initial_sequence_step_must_require_approval(db: Session, ws: Workspace) -> None:
    seq = Sequence(workspace_id=ws.id, name="s")
    db.add(seq)
    db.flush()
    _expect_integrity_error(db, SequenceStep(
        sequence_id=seq.id, step_order=0, message_type=DraftType.INITIAL,
        content_policy=ContentPolicy.GENERATED_AUTO_POLICY, requires_approval=True),
        "ck_sequence_steps_initial_requires_approval")


def _campaign_and_prospect(db: Session, ws: Workspace) -> tuple[Campaign, Prospect]:
    campaign, prospect = Campaign(workspace_id=ws.id, name="c"), Prospect(workspace_id=ws.id)
    db.add_all([campaign, prospect])
    db.flush()
    return campaign, prospect


def _draft(ws: Workspace, campaign: Campaign, prospect: Prospect, **kw: object) -> EmailDraft:
    values: dict[str, object] = dict(workspace_id=ws.id, campaign_id=campaign.id, prospect_id=prospect.id,
                                     draft_type=DraftType.INITIAL, subject="s", body_text="b",
                                     content_sha256="0" * 64)
    values.update(kw)
    return EmailDraft(**values)


def test_approved_draft_requires_approver_and_content_hash(db: Session, ws: Workspace) -> None:
    campaign, prospect = _campaign_and_prospect(db, ws)
    _expect_integrity_error(db, _draft(ws, campaign, prospect, status=DraftStatus.APPROVED),
                            "ck_email_drafts_approval_complete")


def test_only_one_current_draft_version(db: Session, ws: Workspace) -> None:
    campaign, prospect = _campaign_and_prospect(db, ws)
    db.add(_draft(ws, campaign, prospect, version=1))
    db.flush()
    _expect_integrity_error(db, _draft(ws, campaign, prospect, version=2), "uq_email_drafts_current")


def test_scheduled_action_idempotency_key_is_unique(db: Session, ws: Workspace) -> None:
    key = f"send:{uuid.uuid4()}:step:0"
    db.add(ScheduledAction(workspace_id=ws.id, action_type=ScheduledActionType.SEND_EMAIL,
                           idempotency_key=key, due_at=NOW))
    db.flush()
    _expect_integrity_error(db, ScheduledAction(workspace_id=ws.id, action_type=ScheduledActionType.SEND_EMAIL,
                                                idempotency_key=key, due_at=NOW),
                            "uq_scheduled_actions_idempotency_key")


def test_provider_events_are_deduplicated(db: Session) -> None:
    db.add(ProviderEvent(provider=EmailProvider.GMAIL, event_type="history", dedupe_key="gmail:h:42"))
    db.flush()
    _expect_integrity_error(db, ProviderEvent(provider=EmailProvider.GMAIL, event_type="history",
                                              dedupe_key="gmail:h:42"), "uq_provider_events_provider_dedupe_key")


def test_research_run_has_exactly_one_matching_subject(db: Session, ws: Workspace) -> None:
    company = Company(workspace_id=ws.id, name="Acme")
    db.add(company)
    db.flush()
    _expect_integrity_error(db, ResearchRun(workspace_id=ws.id, subject_type=ResearchSubject.PROSPECT,
                                            company_id=company.id), "ck_research_runs_subject_type_matches")


def test_campaign_scoped_suppression_requires_campaign(db: Session, ws: Workspace) -> None:
    _expect_integrity_error(db, Suppression(
        workspace_id=ws.id, email_normalized="a@x.com", scope=SuppressionScope.CAMPAIGN,
        reason=SuppressionReason.MANUAL, source=SuppressionSource.USER), "ck_suppressions_campaign_scope_matches")


# --- Compare-and-set transitions --------------------------------------------------------------

def _action(db: Session, ws: Workspace, state: ScheduledActionState) -> ScheduledAction:
    a = ScheduledAction(workspace_id=ws.id, action_type=ScheduledActionType.SEND_EMAIL,
                        idempotency_key=f"k:{uuid.uuid4()}", due_at=NOW, state=state)
    db.add(a)
    db.flush()
    return a


def test_transition_row_applies_allowed_transition(db: Session, ws: Workspace) -> None:
    a = _action(db, ws, ScheduledActionState.PENDING)
    transition_row(db, ScheduledAction, a.id, SCHEDULED_ACTION, ScheduledActionState.CANCELLED,
                   extra_values={"cancel_reason": "replied"})
    db.refresh(a)
    assert a.state == ScheduledActionState.CANCELLED and a.cancel_reason == "replied"


def test_transition_row_refuses_to_cancel_executing_send(db: Session, ws: Workspace) -> None:
    a = _action(db, ws, ScheduledActionState.EXECUTING)
    with pytest.raises(StaleTransition):
        transition_row(db, ScheduledAction, a.id, SCHEDULED_ACTION, ScheduledActionState.CANCELLED)
    db.refresh(a)
    assert a.state == ScheduledActionState.EXECUTING


def test_transition_row_detects_lost_race(db: Session, ws: Workspace) -> None:
    """Simulates a worker that read PENDING, while reply ingestion cancelled the action in between."""
    a = _action(db, ws, ScheduledActionState.PENDING)
    transition_row(db, ScheduledAction, a.id, SCHEDULED_ACTION, ScheduledActionState.CANCELLED)
    with pytest.raises(StaleTransition):
        transition_row(db, ScheduledAction, a.id, SCHEDULED_ACTION, ScheduledActionState.CLAIMED,
                       expected_from=ScheduledActionState.PENDING)
