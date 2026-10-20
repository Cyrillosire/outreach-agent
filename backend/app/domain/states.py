"""Domain states and their allowed transitions.

These maps are the single source of truth for lifecycle rules. Services must go through
`StateMachine.assert_transition` (and, for persisted rows, `app.db.transitions.transition_row`,
which applies the same rule as an atomic compare-and-set in SQL). Scattered boolean flags are
deliberately avoided.

Safety principle: transitions into "stop" states (replied, suppressed, cancelled, ...) are allowed
from every non-terminal state, so reply ingestion and suppression can always win a race against
scheduled sending. The only state that cannot be cancelled is `ScheduledActionState.EXECUTING`,
because the provider call may already be in flight; that case is resolved by reconciliation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class InvalidTransition(Exception):
    def __init__(self, machine: str, current: str, target: str) -> None:
        super().__init__(f"{machine}: transition {current!r} -> {target!r} is not allowed")
        self.machine = machine
        self.current = current
        self.target = target


@dataclass(frozen=True)
class StateMachine[S: StrEnum]:
    name: str
    states: type[S]
    initial: S
    transitions: Mapping[S, frozenset[S]]
    # Transitions that only a human actor may perform (e.g. resuming outreach after a reply).
    manual_only: frozenset[tuple[S, S]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        missing = set(self.states) - set(self.transitions)
        if missing:
            raise ValueError(f"{self.name}: states without a transition entry: {sorted(missing)}")

    def allowed_from(self, current: S) -> frozenset[S]:
        return self.transitions[current]

    def can_transition(self, current: S, target: S, *, by_human: bool = False) -> bool:
        if target not in self.transitions[current]:
            return False
        return by_human or (current, target) not in self.manual_only

    def assert_transition(self, current: S, target: S, *, by_human: bool = False) -> None:
        if not self.can_transition(current, target, by_human=by_human):
            raise InvalidTransition(self.name, current.value, target.value)

    def predecessors(self, target: S, *, by_human: bool = False) -> frozenset[S]:
        """States from which `target` is reachable in one step (used to build SQL compare-and-set)."""
        return frozenset(s for s in self.states if self.can_transition(s, target, by_human=by_human))

    @property
    def terminal_states(self) -> frozenset[S]:
        return frozenset(s for s, targets in self.transitions.items() if not targets)


def _fs[S: StrEnum](*items: S) -> frozenset[S]:
    return frozenset(items)


def _plus[S: StrEnum](base: Iterable[S], *extra: S) -> frozenset[S]:
    return frozenset(base) | frozenset(extra)


# --- Prospect research -------------------------------------------------------------------------

class ResearchStatus(StrEnum):
    PENDING = "pending"
    RESEARCHING_COMPANY = "researching_company"
    RESEARCHING_INDIVIDUAL = "researching_individual"
    VERIFYING = "verifying"
    READY = "ready_for_personalization"
    NEEDS_REVIEW = "needs_review"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED_RETRYABLE = "failed_retryable"


_R = ResearchStatus
_RESEARCH_OUTCOMES = _fs(_R.READY, _R.NEEDS_REVIEW, _R.INSUFFICIENT_EVIDENCE, _R.FAILED_RETRYABLE)
RESEARCH = StateMachine(
    name="research",
    states=ResearchStatus,
    initial=_R.PENDING,
    transitions={
        _R.PENDING: _fs(_R.RESEARCHING_COMPANY, _R.RESEARCHING_INDIVIDUAL, _R.FAILED_RETRYABLE),
        # Company and individual branches run concurrently; the displayed stage may move between them.
        _R.RESEARCHING_COMPANY: _fs(_R.RESEARCHING_INDIVIDUAL, _R.VERIFYING, _R.FAILED_RETRYABLE),
        _R.RESEARCHING_INDIVIDUAL: _fs(_R.RESEARCHING_COMPANY, _R.VERIFYING, _R.FAILED_RETRYABLE),
        _R.VERIFYING: _RESEARCH_OUTCOMES,
        # Refresh / manual resolution paths. Nothing is terminal: research can always be redone.
        _R.READY: _fs(_R.PENDING, _R.NEEDS_REVIEW),
        _R.NEEDS_REVIEW: _fs(_R.READY, _R.INSUFFICIENT_EVIDENCE, _R.PENDING),
        _R.INSUFFICIENT_EVIDENCE: _fs(_R.PENDING),
        _R.FAILED_RETRYABLE: _fs(_R.PENDING),
    },
    manual_only=frozenset({(_R.NEEDS_REVIEW, _R.READY)}),
)


# --- Draft approval ----------------------------------------------------------------------------

class DraftStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    # Replaced by a newer version (edit or regeneration). Approval never carries over to a new version.
    SUPERSEDED = "superseded"


_D = DraftStatus
DRAFT = StateMachine(
    name="draft",
    states=DraftStatus,
    initial=_D.PENDING_REVIEW,
    transitions={
        _D.PENDING_REVIEW: _fs(_D.APPROVED, _D.REJECTED, _D.SUPERSEDED),
        _D.APPROVED: _fs(_D.SUPERSEDED),
        _D.REJECTED: _fs(_D.SUPERSEDED),
        _D.SUPERSEDED: _fs(),
    },
    manual_only=frozenset({(_D.PENDING_REVIEW, _D.APPROVED), (_D.PENDING_REVIEW, _D.REJECTED)}),
)


# --- Campaign ----------------------------------------------------------------------------------

class CampaignStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_C = CampaignStatus
CAMPAIGN = StateMachine(
    name="campaign",
    states=CampaignStatus,
    initial=_C.DRAFT,
    transitions={
        _C.DRAFT: _fs(_C.ACTIVE, _C.CANCELLED),
        _C.ACTIVE: _fs(_C.PAUSED, _C.COMPLETED, _C.CANCELLED),
        _C.PAUSED: _fs(_C.ACTIVE, _C.CANCELLED),
        _C.COMPLETED: _fs(),
        _C.CANCELLED: _fs(),
    },
    manual_only=frozenset({(_C.DRAFT, _C.ACTIVE), (_C.PAUSED, _C.ACTIVE)}),
)


# --- Campaign membership (per prospect sequence state) -----------------------------------------

class EnrollmentState(StrEnum):
    ENROLLED = "enrolled"
    AWAITING_DRAFT = "awaiting_draft"
    AWAITING_APPROVAL = "awaiting_approval"
    SCHEDULED = "scheduled"
    IN_SEQUENCE = "in_sequence"
    REPLIED = "replied"
    PAUSED = "paused"
    COMPLETED = "completed"
    SUPPRESSED = "suppressed"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"
    CANCELLED = "cancelled"


_E = EnrollmentState
_E_STOPS = (_E.REPLIED, _E.SUPPRESSED, _E.BOUNCED, _E.UNSUBSCRIBED, _E.CANCELLED)
ENROLLMENT = StateMachine(
    name="enrollment",
    states=EnrollmentState,
    initial=_E.ENROLLED,
    transitions={
        _E.ENROLLED: _plus(_E_STOPS, _E.AWAITING_DRAFT, _E.PAUSED),
        _E.AWAITING_DRAFT: _plus(_E_STOPS, _E.AWAITING_APPROVAL, _E.PAUSED),
        _E.AWAITING_APPROVAL: _plus(_E_STOPS, _E.AWAITING_DRAFT, _E.SCHEDULED, _E.PAUSED),
        _E.SCHEDULED: _plus(_E_STOPS, _E.IN_SEQUENCE, _E.AWAITING_APPROVAL, _E.PAUSED),
        _E.IN_SEQUENCE: _plus(_E_STOPS, _E.AWAITING_APPROVAL, _E.COMPLETED, _E.PAUSED),
        _E.PAUSED: _plus(_E_STOPS, _E.AWAITING_DRAFT, _E.AWAITING_APPROVAL, _E.SCHEDULED, _E.IN_SEQUENCE),
        # A reply halts automation. Only a human may resume (or close) the sequence after review.
        _E.REPLIED: _fs(_E.IN_SEQUENCE, _E.COMPLETED, _E.SUPPRESSED, _E.UNSUBSCRIBED, _E.CANCELLED),
        # Suppression may be lifted by a human under an explicit review policy; it never auto-expires.
        _E.SUPPRESSED: _fs(_E.PAUSED, _E.CANCELLED),
        _E.COMPLETED: _fs(),
        _E.BOUNCED: _fs(),
        _E.UNSUBSCRIBED: _fs(),
        _E.CANCELLED: _fs(),
    },
    manual_only=frozenset(
        {(_E.REPLIED, _E.IN_SEQUENCE), (_E.REPLIED, _E.COMPLETED), (_E.SUPPRESSED, _E.PAUSED)}
        # Leaving PAUSED for any non-stop state is a human decision.
        | {(_E.PAUSED, s) for s in (_E.AWAITING_DRAFT, _E.AWAITING_APPROVAL, _E.SCHEDULED, _E.IN_SEQUENCE)}
    ),
)
ENROLLMENT_SENDABLE = _fs(_E.SCHEDULED, _E.IN_SEQUENCE)


# --- Scheduled actions (durable outbox for sends and other timed work) -------------------------

class ScheduledActionState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    # Provider call outcome unknown (timeout, crash mid-call). Must be reconciled, never blindly retried.
    AMBIGUOUS = "ambiguous"
    DEAD = "dead"
    CANCELLED = "cancelled"


_A = ScheduledActionState
SCHEDULED_ACTION = StateMachine(
    name="scheduled_action",
    states=ScheduledActionState,
    initial=_A.PENDING,
    transitions={
        _A.PENDING: _fs(_A.CLAIMED, _A.CANCELLED),
        # CLAIMED -> PENDING is lease expiry/release before any external side effect happened.
        _A.CLAIMED: _fs(_A.EXECUTING, _A.PENDING, _A.CANCELLED),
        _A.EXECUTING: _fs(_A.SUCCEEDED, _A.FAILED_RETRYABLE, _A.AMBIGUOUS, _A.DEAD),
        _A.FAILED_RETRYABLE: _fs(_A.PENDING, _A.DEAD, _A.CANCELLED),
        _A.AMBIGUOUS: _fs(_A.SUCCEEDED, _A.FAILED_RETRYABLE, _A.DEAD),
        _A.SUCCEEDED: _fs(),
        _A.DEAD: _fs(),
        _A.CANCELLED: _fs(),
    },
)


# --- Outbound message status -------------------------------------------------------------------

class OutboundStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    # The provider accepted the API request. This is NOT a delivery guarantee.
    PROVIDER_ACCEPTED = "provider_accepted"
    UNKNOWN = "unknown"
    FAILED = "failed"
    BOUNCED = "bounced"


_O = OutboundStatus
OUTBOUND = StateMachine(
    name="outbound_message",
    states=OutboundStatus,
    initial=_O.QUEUED,
    transitions={
        _O.QUEUED: _fs(_O.SENDING, _O.FAILED),
        # SENDING -> QUEUED only when the provider call provably never reached the provider (connection refused,
        # or a Graph message confirmed still a draft), so a retry can't duplicate the email.
        _O.SENDING: _fs(_O.PROVIDER_ACCEPTED, _O.UNKNOWN, _O.FAILED, _O.QUEUED),
        _O.UNKNOWN: _fs(_O.PROVIDER_ACCEPTED, _O.FAILED),
        _O.PROVIDER_ACCEPTED: _fs(_O.BOUNCED),
        _O.FAILED: _fs(),
        _O.BOUNCED: _fs(),
    },
)


# --- Email account connection ------------------------------------------------------------------

class EmailAccountStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    DISCONNECTED = "disconnected"


_X = EmailAccountStatus
EMAIL_ACCOUNT = StateMachine(
    name="email_account",
    states=EmailAccountStatus,
    initial=_X.CONNECTED,
    transitions={
        _X.CONNECTED: _fs(_X.NEEDS_REAUTH, _X.ERROR, _X.DISCONNECTED),
        _X.NEEDS_REAUTH: _fs(_X.CONNECTED, _X.DISCONNECTED),
        _X.ERROR: _fs(_X.CONNECTED, _X.NEEDS_REAUTH, _X.DISCONNECTED),
        _X.DISCONNECTED: _fs(_X.CONNECTED),
    },
)


# --- Simple enums without lifecycle rules ------------------------------------------------------

class UserRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class EmailProvider(StrEnum):
    GMAIL = "gmail"
    MICROSOFT = "microsoft"


class ImportStatus(StrEnum):
    UPLOADED = "uploaded"
    # Column mapping applied and every row validated. Re-mapping keeps the import in this state.
    VALIDATED = "validated"
    # Transient: held only inside the commit transaction (row-locked), so concurrent commits serialize.
    IMPORTING = "importing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_I = ImportStatus
IMPORT = StateMachine(
    name="import",
    states=ImportStatus,
    initial=_I.UPLOADED,
    transitions={
        _I.UPLOADED: _fs(_I.VALIDATED, _I.FAILED, _I.CANCELLED),
        _I.VALIDATED: _fs(_I.IMPORTING, _I.CANCELLED),
        _I.IMPORTING: _fs(_I.COMPLETED, _I.FAILED),
        _I.COMPLETED: _fs(),
        _I.FAILED: _fs(),
        _I.CANCELLED: _fs(),
    },
)
# States in which the column mapping may be (re)applied.
IMPORT_MAPPABLE = _fs(_I.UPLOADED, _I.VALIDATED)


class ImportRowStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    DUPLICATE_IN_FILE = "duplicate_in_file"
    ALREADY_EXISTS = "already_exists"
    IMPORTED = "imported"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    # Permanent failure (non-retryable error, e.g. invalid input or missing configuration).
    FAILED = "failed"
    # Retries exhausted.
    DEAD = "dead"
    CANCELLED = "cancelled"


_J = JobState
JOB = StateMachine(
    name="job",
    states=JobState,
    initial=_J.QUEUED,
    transitions={
        _J.QUEUED: _fs(_J.RUNNING, _J.CANCELLED),
        # RUNNING -> QUEUED is a scheduled retry or a reclaimed expired lease.
        _J.RUNNING: _fs(_J.SUCCEEDED, _J.QUEUED, _J.FAILED, _J.DEAD),
        _J.SUCCEEDED: _fs(),
        _J.FAILED: _fs(),
        _J.DEAD: _fs(),
        _J.CANCELLED: _fs(),
    },
)
JOB_ACTIVE = _fs(_J.QUEUED, _J.RUNNING)


class IdempotencyState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class EmailVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    SYNTAX_VALID = "syntax_valid"
    INVALID = "invalid"
    BOUNCED = "bounced"


class ResearchSubject(StrEnum):
    COMPANY = "company"
    PROSPECT = "prospect"


class ResearchRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FactKind(StrEnum):
    VERIFIED_FACT = "verified_fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    MISSING = "missing"
    CONFLICTING = "conflicting"


class FactVerification(StrEnum):
    UNVERIFIED = "unverified"
    SINGLE_SOURCE = "single_source"
    CORROBORATED = "corroborated"
    CONTRADICTED = "contradicted"
    REJECTED = "rejected"


class SourceType(StrEnum):
    OFFICIAL_WEBSITE = "official_website"
    PRESS_RELEASE = "press_release"
    ANNUAL_REPORT = "annual_report"
    NEWS = "news"
    PROFESSIONAL_PROFILE = "professional_profile"
    PUBLICATION = "publication"
    GOVERNMENT = "government"
    # The prospect's own LinkedIn presence, tied to them by the profile URL on their record (never fetched from
    # linkedin.com: found through search indexes, or captured by a user from their own browser).
    LINKEDIN_PROFILE = "linkedin_profile"
    LINKEDIN_POST = "linkedin_post"
    LINKEDIN_ACTIVITY = "linkedin_activity"
    OTHER = "other"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    BLOCKED = "blocked"
    FAILED = "failed"


class DraftType(StrEnum):
    INITIAL = "initial"
    FOLLOW_UP = "follow_up"


class OfferingType(StrEnum):
    PRODUCT = "product"
    SERVICE = "service"
    PARTNERSHIP_PROGRAM = "partnership_program"
    PILOT_PROGRAM = "pilot_program"
    EVENT = "event"
    RESEARCH_COLLABORATION = "research_collaboration"
    OTHER = "other"


class OutreachMotion(StrEnum):
    """How the campaign approaches prospects. Each motion has its own drafting playbook."""

    PRODUCT_SALE = "product_sale"
    PARTNERSHIP_PROPOSAL = "partnership_proposal"
    PILOT_OFFER = "pilot_offer"
    SERVICE_ENGAGEMENT = "service_engagement"
    EVENT_INVITATION = "event_invitation"
    RESEARCH_OR_FEEDBACK = "research_or_feedback"


class Fit(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    DISQUALIFIED = "disqualified"


class ContentPolicy(StrEnum):
    MANUAL_APPROVED = "manual_approved"
    GENERATED_REQUIRES_APPROVAL = "generated_requires_approval"
    # Explicit opt-in per sequence step; generation is still validated and fully auditable.
    GENERATED_AUTO_POLICY = "generated_auto_policy"


class MessageDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class ReplyClassification(StrEnum):
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    REFERRAL = "referral"
    OUT_OF_OFFICE = "out_of_office"
    UNSUBSCRIBE_REQUEST = "unsubscribe_request"
    BOUNCE = "bounce"
    OTHER = "other"
    UNCLASSIFIED = "unclassified"


class ScheduledActionType(StrEnum):
    SEND_EMAIL = "send_email"
    GENERATE_FOLLOW_UP = "generate_follow_up"
    RECONCILE_SEND = "reconcile_send"


class SuppressionScope(StrEnum):
    WORKSPACE = "workspace"
    CAMPAIGN = "campaign"


class SuppressionReason(StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    REPLIED = "replied"
    MANUAL = "manual"
    LEGAL = "legal"


class SuppressionSource(StrEnum):
    USER = "user"
    SYSTEM = "system"
    PROVIDER = "provider"
    RECIPIENT = "recipient"


class ProviderEventState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    WORKER = "worker"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
