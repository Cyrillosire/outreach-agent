"""Import every model so Base.metadata is complete (used by Alembic and tests)."""

from app.models.campaigns import Campaign, CampaignProspect, Sequence, SequenceStep
from app.models.email import EmailAccount, EmailDraft, EmailDraftEvidence, EmailMessage
from app.models.identity import User, UserSession, Workspace
from app.models.operations import AuditLog, IdempotencyKey, ProviderEvent, ScheduledAction, Suppression
from app.models.prospects import Company, Prospect, ProspectImport, ProspectImportRow
from app.models.research import ResearchFact, ResearchFactEvidence, ResearchRun, ResearchSource

__all__ = [
    "AuditLog",
    "Campaign",
    "CampaignProspect",
    "Company",
    "EmailAccount",
    "EmailDraft",
    "EmailDraftEvidence",
    "EmailMessage",
    "IdempotencyKey",
    "Prospect",
    "ProspectImport",
    "ProspectImportRow",
    "ProviderEvent",
    "ResearchFact",
    "ResearchFactEvidence",
    "ResearchRun",
    "ResearchSource",
    "ScheduledAction",
    "Sequence",
    "SequenceStep",
    "Suppression",
    "User",
    "UserSession",
    "Workspace",
]
