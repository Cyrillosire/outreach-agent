from collections import deque

import pytest

from app.domain.states import (
    CAMPAIGN,
    DRAFT,
    EMAIL_ACCOUNT,
    ENROLLMENT,
    IMPORT,
    OUTBOUND,
    RESEARCH,
    SCHEDULED_ACTION,
    CampaignStatus,
    DraftStatus,
    EnrollmentState,
    InvalidTransition,
    OutboundStatus,
    ResearchStatus,
    ScheduledActionState,
    StateMachine,
)

ALL_MACHINES = [RESEARCH, DRAFT, CAMPAIGN, ENROLLMENT, SCHEDULED_ACTION, OUTBOUND, EMAIL_ACCOUNT, IMPORT]


def _reachable(machine: StateMachine) -> set:  # type: ignore[type-arg]
    seen, queue = {machine.initial}, deque([machine.initial])
    while queue:
        for nxt in machine.allowed_from(queue.popleft()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


@pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
def test_every_state_is_reachable_from_initial(machine: StateMachine) -> None:  # type: ignore[type-arg]
    assert _reachable(machine) == set(machine.states)


@pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
def test_no_self_transitions(machine: StateMachine) -> None:  # type: ignore[type-arg]
    for state, targets in machine.transitions.items():
        assert state not in targets


@pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
def test_manual_only_pairs_are_real_transitions(machine: StateMachine) -> None:  # type: ignore[type-arg]
    for src, dst in machine.manual_only:
        assert dst in machine.transitions[src]


def test_machine_requires_entry_for_every_state() -> None:
    with pytest.raises(ValueError, match="without a transition entry"):
        StateMachine(name="bad", states=DraftStatus, initial=DraftStatus.PENDING_REVIEW,
                     transitions={DraftStatus.PENDING_REVIEW: frozenset()})


# --- Enrollment: replies and suppression must always be able to win -----------------------------

STOP_STATES = [EnrollmentState.REPLIED, EnrollmentState.SUPPRESSED, EnrollmentState.UNSUBSCRIBED,
               EnrollmentState.BOUNCED, EnrollmentState.CANCELLED]
ACTIVE_ENROLLMENT = [EnrollmentState.ENROLLED, EnrollmentState.AWAITING_DRAFT,
                     EnrollmentState.AWAITING_APPROVAL, EnrollmentState.SCHEDULED,
                     EnrollmentState.IN_SEQUENCE, EnrollmentState.PAUSED]


@pytest.mark.parametrize("current", ACTIVE_ENROLLMENT)
@pytest.mark.parametrize("stop", STOP_STATES)
def test_stop_states_reachable_from_every_active_enrollment_state(current, stop) -> None:  # type: ignore[no-untyped-def]
    assert ENROLLMENT.can_transition(current, stop)  # automated (non-human) actor


def test_resuming_after_reply_requires_human() -> None:
    assert not ENROLLMENT.can_transition(EnrollmentState.REPLIED, EnrollmentState.IN_SEQUENCE)
    assert ENROLLMENT.can_transition(EnrollmentState.REPLIED, EnrollmentState.IN_SEQUENCE, by_human=True)
    with pytest.raises(InvalidTransition):
        ENROLLMENT.assert_transition(EnrollmentState.REPLIED, EnrollmentState.SCHEDULED, by_human=True)


def test_unpausing_requires_human() -> None:
    assert not ENROLLMENT.can_transition(EnrollmentState.PAUSED, EnrollmentState.IN_SEQUENCE)
    assert ENROLLMENT.can_transition(EnrollmentState.PAUSED, EnrollmentState.IN_SEQUENCE, by_human=True)


@pytest.mark.parametrize("terminal", [EnrollmentState.BOUNCED, EnrollmentState.UNSUBSCRIBED,
                                      EnrollmentState.COMPLETED, EnrollmentState.CANCELLED])
def test_hard_stops_are_terminal(terminal: EnrollmentState) -> None:
    assert terminal in ENROLLMENT.terminal_states


def test_suppression_never_auto_lifts() -> None:
    for target in ENROLLMENT.allowed_from(EnrollmentState.SUPPRESSED):
        assert target in (EnrollmentState.CANCELLED,) or not ENROLLMENT.can_transition(
            EnrollmentState.SUPPRESSED, target)


# --- Scheduled actions ------------------------------------------------------------------------

def test_executing_action_cannot_be_cancelled() -> None:
    """Once the provider call may be in flight, cancellation is impossible; reconciliation decides."""
    assert not SCHEDULED_ACTION.can_transition(ScheduledActionState.EXECUTING, ScheduledActionState.CANCELLED)


def test_ambiguous_send_cannot_return_to_pending() -> None:
    """An unknown-outcome send must be reconciled, never silently re-queued."""
    assert ScheduledActionState.PENDING not in SCHEDULED_ACTION.allowed_from(ScheduledActionState.AMBIGUOUS)
    assert ScheduledActionState.CLAIMED not in SCHEDULED_ACTION.allowed_from(ScheduledActionState.AMBIGUOUS)


def test_cancellation_predecessors() -> None:
    assert SCHEDULED_ACTION.predecessors(ScheduledActionState.CANCELLED) == {
        ScheduledActionState.PENDING, ScheduledActionState.CLAIMED, ScheduledActionState.FAILED_RETRYABLE}


# --- Drafts, campaigns, outbound, research ----------------------------------------------------

def test_approval_requires_human_and_never_reverts() -> None:
    assert not DRAFT.can_transition(DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED)
    assert DRAFT.can_transition(DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED, by_human=True)
    assert DRAFT.allowed_from(DraftStatus.APPROVED) == {DraftStatus.SUPERSEDED}
    assert DraftStatus.SUPERSEDED in DRAFT.terminal_states


def test_campaign_cancel_is_final_and_activation_is_manual() -> None:
    assert CampaignStatus.CANCELLED in CAMPAIGN.terminal_states
    assert not CAMPAIGN.can_transition(CampaignStatus.DRAFT, CampaignStatus.ACTIVE)
    assert not CAMPAIGN.can_transition(CampaignStatus.PAUSED, CampaignStatus.ACTIVE)
    with pytest.raises(InvalidTransition):
        CAMPAIGN.assert_transition(CampaignStatus.CANCELLED, CampaignStatus.ACTIVE, by_human=True)


def test_outbound_has_no_delivered_state_and_unknown_needs_resolution() -> None:
    assert "delivered" not in {s.value for s in OutboundStatus}
    assert OUTBOUND.allowed_from(OutboundStatus.UNKNOWN) == {
        OutboundStatus.PROVIDER_ACCEPTED, OutboundStatus.FAILED}
    assert OutboundStatus.SENDING not in OUTBOUND.allowed_from(OutboundStatus.UNKNOWN)


def test_research_cannot_skip_verification_to_ready() -> None:
    for state in (ResearchStatus.PENDING, ResearchStatus.RESEARCHING_COMPANY,
                  ResearchStatus.RESEARCHING_INDIVIDUAL):
        assert ResearchStatus.READY not in RESEARCH.allowed_from(state)
    assert not RESEARCH.can_transition(ResearchStatus.NEEDS_REVIEW, ResearchStatus.READY)
    assert RESEARCH.can_transition(ResearchStatus.NEEDS_REVIEW, ResearchStatus.READY, by_human=True)


def test_invalid_transition_message() -> None:
    with pytest.raises(InvalidTransition, match="draft: transition 'superseded' -> 'approved'"):
        DRAFT.assert_transition(DraftStatus.SUPERSEDED, DraftStatus.APPROVED, by_human=True)
