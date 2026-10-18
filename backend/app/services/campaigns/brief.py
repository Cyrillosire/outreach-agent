"""Structured definitions of WHAT we sell (offerings) and HOW a campaign approaches an audience (briefs).

Everything here becomes input to drafting, so the structure is deliberate:
- Offering facts are seller-vouched. Drafts may state an offering claim only if it is one of these items (proof
  points are cited by id and validated); `forbidden_claims` are hard-blocked.
- The campaign brief defines audience and personas, fit signals (what in the research indicates relevance),
  disqualifiers, the call to action, style limits, and the sender identity and compliance footer. The footer and
  signature are inserted by code, never written by the model.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.domain.states import OfferingType, OutreachMotion

ShortText = str  # alias for readability in schemas below


def _clean_list(values: list[str], *, max_items: int, max_len: int) -> list[str]:
    out: list[str] = []
    for v in values:
        v = " ".join(v.split())
        if v and v not in out:
            out.append(v[:max_len])
    return out[:max_items]


# --- Offering --------------------------------------------------------------------------------------

class ProofKind(StrEnum):
    METRIC = "metric"              # e.g. "cuts documentation time by 40% (2025 pilot, 3 hospitals)"
    CASE_STUDY = "case_study"
    CUSTOMER = "customer"          # named customer, only if you have permission to name them
    CERTIFICATION = "certification"
    AWARD = "award"
    OTHER = "other"


class ProofPoint(BaseModel):
    id: str = Field(default_factory=lambda: "pp_" + secrets.token_hex(3), pattern=r"^pp_[a-z0-9]{4,12}$")
    claim: str = Field(min_length=5, max_length=300)
    kind: ProofKind = ProofKind.OTHER
    source: str | None = Field(default=None, max_length=500, description="Where this is documented (internal note)")
    can_name_publicly: bool = True


class OfferingDetails(BaseModel):
    problems_solved: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    proof_points: list[ProofPoint] = Field(default_factory=list, max_length=20)
    forbidden_claims: list[str] = Field(default_factory=list, description="Claims drafts must never make")
    internal_notes: str | None = Field(default=None, max_length=4000, description="Never shown to prospects")

    @field_validator("problems_solved", "capabilities", "differentiators", "forbidden_claims")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_list(v, max_items=15, max_len=300)

    @model_validator(mode="after")
    def _unique_proof_ids(self) -> OfferingDetails:
        ids = [p.id for p in self.proof_points]
        if len(ids) != len(set(ids)):
            raise ValueError("proof point ids must be unique")
        return self


# --- Campaign brief ----------------------------------------------------------------------------------

class Persona(BaseModel):
    name: str = Field(min_length=2, max_length=100, description='e.g. "Hospital CIO / IT head"')
    title_keywords: list[str] = Field(default_factory=list, description="Job-title words that identify this persona")
    pains: list[str] = Field(default_factory=list, description="What this role struggles with (as you understand it)")
    priorities: list[str] = Field(default_factory=list, description="What this role is measured on")
    angle: str | None = Field(default=None, max_length=400, description="How to frame the offering for this role")

    @field_validator("title_keywords", "pains", "priorities")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_list(v, max_items=12, max_len=200)


class Audience(BaseModel):
    industries: list[str] = Field(default_factory=list)
    organization_types: list[str] = Field(default_factory=list, description='e.g. "multi-specialty hospitals"')
    regions: list[str] = Field(default_factory=list)
    size_notes: str | None = Field(default=None, max_length=300)
    personas: list[Persona] = Field(default_factory=list, max_length=8)

    @field_validator("industries", "organization_types", "regions")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_list(v, max_items=20, max_len=120)


class CtaType(StrEnum):
    BOOK_MEETING = "book_meeting"
    REPLY_INTEREST = "reply_interest"
    REQUEST_PILOT = "request_pilot"
    SHARE_MATERIALS = "share_materials"
    EVENT_RSVP = "event_rsvp"
    REFERRAL = "referral"


class CallToAction(BaseModel):
    type: CtaType = CtaType.BOOK_MEETING
    details: str = Field(default="", max_length=300, description='e.g. "a 20-minute call next week"')
    link: HttpUrl | None = Field(default=None, description="Optional scheduling/RSVP link (one link at most)")


class Style(BaseModel):
    tone: Literal["formal", "professional", "warm", "direct"] = "professional"
    max_words: int = Field(default=130, ge=50, le=250)
    language: str = Field(default="English", max_length=40)
    greeting: str = Field(default="Hi {first_name},", max_length=60)
    sign_off: str = Field(default="Best regards,", max_length=60)
    banned_phrases: list[str] = Field(default_factory=list)

    @field_validator("banned_phrases")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_list(v, max_items=30, max_len=100)


class Sender(BaseModel):
    name: str = Field(default="", max_length=120)
    title: str = Field(default="", max_length=120)
    company: str = Field(default="", max_length=200)
    signature: str = Field(default="", max_length=600, description="Lines under the sign-off (name, title, phone)")
    compliance_footer: str = Field(default="", max_length=600,
                                   description="Legal sender identity, postal address, and how to opt out")


class SequencePolicy(BaseModel):
    draft_followups: Literal["after_initial_approved", "upfront", "manual"] = "after_initial_approved"


class CampaignBrief(BaseModel):
    schema_version: Literal[1] = 1
    goal: str = Field(default="", max_length=500, description="What success looks like for this campaign")
    why_now: str | None = Field(default=None, max_length=500,
                                description="A TRUE campaign-wide timing reason (a regulation, a season, an event)")
    audience: Audience = Field(default_factory=Audience)
    fit_signals: list[str] = Field(default_factory=list,
                                   description="Research topics that make a prospect relevant (e.g. 'HIS rollout')")
    disqualifiers: list[str] = Field(default_factory=list, description="Situations where we should NOT reach out")
    value_focus: list[str] = Field(default_factory=list,
                                   description="Which offering problems/capabilities to emphasise in this campaign")
    cta: CallToAction = Field(default_factory=CallToAction)
    style: Style = Field(default_factory=Style)
    sender: Sender = Field(default_factory=Sender)
    sequence: SequencePolicy = Field(default_factory=SequencePolicy)
    allow_company_only_personalization: bool = Field(
        default=False,
        description="Also draft for prospects whose own role couldn't be confirmed (company evidence only)")

    @field_validator("fit_signals", "disqualifiers", "value_focus")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_list(v, max_items=20, max_len=200)


# --- Readiness ---------------------------------------------------------------------------------------

class ReadinessItem(BaseModel):
    field: str
    message: str
    severity: Literal["blocker", "recommended"]


def readiness(brief: CampaignBrief, offering: OfferingDetails | None, *, offering_set: bool,
              steps: int) -> list[ReadinessItem]:
    """What's missing before drafts can be generated (blockers) or would be weaker without (recommended)."""
    items: list[ReadinessItem] = []

    def need(cond: bool, field: str, message: str, severity: Literal["blocker", "recommended"] = "blocker") -> None:
        if not cond:
            items.append(ReadinessItem(field=field, message=message, severity=severity))

    need(offering_set, "offering", "Choose what this campaign sells (an offering).")
    need(bool(brief.goal.strip()), "goal", "Describe the campaign goal (what a successful reply looks like).")
    need(bool(brief.audience.personas), "audience.personas", "Add at least one target persona.")
    need(bool(brief.cta.details.strip()), "cta.details", "Describe the call to action (e.g. 'a 20-minute call').")
    need(bool(brief.sender.name.strip()), "sender.name", "Add the sender's name.")
    need(bool(brief.sender.compliance_footer.strip()), "sender.compliance_footer",
         "Add a compliance footer: your organization's legal name, postal address, and how to opt out.")
    need(steps >= 1, "sequence", "The sequence needs at least the initial email step.")
    if offering is not None:
        need(bool(offering.problems_solved or offering.capabilities), "offering.details",
             "Describe the problems the offering solves or its capabilities, or drafts will be vague.")
        need(bool(offering.proof_points), "offering.proof_points",
             "Add proof points (metrics, case studies). Without them the value proposition can't be specific.",
             "recommended")
    need(bool(brief.fit_signals), "fit_signals",
         "Add fit signals so the strongest, most relevant research is chosen for each prospect.", "recommended")
    need(bool(brief.disqualifiers), "disqualifiers",
         "Add disqualifiers so poor-fit prospects are held back for review instead of emailed.", "recommended")
    return items


def is_ready(items: list[ReadinessItem]) -> bool:
    return not any(i.severity == "blocker" for i in items)


__all__ = ["CampaignBrief", "OfferingDetails", "OfferingType", "OutreachMotion", "ProofPoint", "ShortText",
           "is_ready", "readiness"]
