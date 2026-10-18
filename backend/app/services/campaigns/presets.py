"""Playbooks per outreach motion: sensible defaults for a new campaign and the drafting guidance for the model.

The guidance shapes structure and tone only; it never adds facts. Facts come from research (prospect/company)
and from the offering (seller-vouched proof points).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.states import OutreachMotion
from app.services.campaigns.brief import CallToAction, CampaignBrief, CtaType, Style


@dataclass(frozen=True)
class Playbook:
    motion: OutreachMotion
    label: str
    description: str
    guidance: str
    cta: CallToAction
    style: Style
    example_fit_signals: list[str] = field(default_factory=list)
    example_disqualifiers: list[str] = field(default_factory=list)
    followup_days: tuple[int, ...] = (3, 7, 12)


PLAYBOOKS: dict[OutreachMotion, Playbook] = {
    OutreachMotion.PRODUCT_SALE: Playbook(
        OutreachMotion.PRODUCT_SALE, "Product / solution sale",
        "Sell a product or software to organizations that have the problem it solves.",
        "Open with a specific, verified observation about their organization. Connect it to a problem this persona "
        "owns, phrased as a question unless the research states it. Offer the most relevant capability with one "
        "proof point. Close with a single low-friction ask. No feature lists.",
        CallToAction(type=CtaType.BOOK_MEETING, details="a 20-minute call to see whether it's relevant"),
        Style(tone="professional", max_words=130),
        ["digital transformation", "new system implementation", "expansion", "regulatory change"],
        ["already uses a direct competitor", "too small for the offering"]),
    OutreachMotion.PARTNERSHIP_PROPOSAL: Playbook(
        OutreachMotion.PARTNERSHIP_PROPOSAL, "Partnership opportunity",
        "Propose a partnership (distribution, integration, co-marketing, referral) with complementary organizations.",
        "Lead with what you know about their strengths from research. Explain the complementary fit and what BOTH "
        "sides gain, concretely. Never imply a partnership, endorsement, or prior relationship exists. Propose an "
        "exploratory conversation, not a deal.",
        CallToAction(type=CtaType.BOOK_MEETING, details="an exploratory 30-minute conversation"),
        Style(tone="warm", max_words=150),
        ["serves the same customers", "expanding into new regions", "complementary product line"],
        ["direct competitor", "exclusive partnership with a competitor"]),
    OutreachMotion.PILOT_OFFER: Playbook(
        OutreachMotion.PILOT_OFFER, "Pilot / trial offer",
        "Offer a low-risk, time-boxed pilot with clear success criteria.",
        "Anchor on a specific situation from research. Make the pilot concrete: scope, duration, effort required "
        "from them, and how success will be measured. Emphasize low risk without overpromising outcomes.",
        CallToAction(type=CtaType.REQUEST_PILOT, details="a short call to scope a 4-week pilot"),
        Style(tone="direct", max_words=140)),
    OutreachMotion.SERVICE_ENGAGEMENT: Playbook(
        OutreachMotion.SERVICE_ENGAGEMENT, "Service / consulting engagement",
        "Offer expertise or services (implementation, advisory, managed services).",
        "Show you understand their current initiative from research. Describe relevant experience and the outcome "
        "it produced (proof points only). Offer a small first step such as a diagnostic or assessment.",
        CallToAction(type=CtaType.BOOK_MEETING, details="a 30-minute discovery call"),
        Style(tone="professional", max_words=140)),
    OutreachMotion.EVENT_INVITATION: Playbook(
        OutreachMotion.EVENT_INVITATION, "Event invitation",
        "Invite prospects to a webinar, roundtable, or in-person event.",
        "Explain why this event is relevant to their role and situation. Give the essentials (what, when, where or "
        "online, who else attends) from the offering details. No sales pitch. Ask them to RSVP.",
        CallToAction(type=CtaType.EVENT_RSVP, details="reply to reserve a seat"),
        Style(tone="warm", max_words=120), followup_days=(4, 9)),
    OutreachMotion.RESEARCH_OR_FEEDBACK: Playbook(
        OutreachMotion.RESEARCH_OR_FEEDBACK, "Research / feedback request",
        "Ask experts for their perspective (customer discovery, advisory input).",
        "Explain briefly why their specific experience matters, citing research. Ask a focused question or request a "
        "short conversation. Do not sell; respect their time explicitly.",
        CallToAction(type=CtaType.BOOK_MEETING, details="a 15-minute conversation to hear your perspective"),
        Style(tone="warm", max_words=110), followup_days=(5,)),
}


def default_brief(motion: OutreachMotion) -> CampaignBrief:
    pb = PLAYBOOKS[motion]
    return CampaignBrief(cta=pb.cta.model_copy(), style=pb.style.model_copy(),
                         fit_signals=list(pb.example_fit_signals), disqualifiers=list(pb.example_disqualifiers))
