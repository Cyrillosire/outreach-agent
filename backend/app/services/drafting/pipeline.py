"""Drafting job: write -> validate -> AI review -> regenerate with feedback -> save a versioned, evidence-linked draft.

Never approves anything. Weak or disqualified fits are held for a human decision (unless forced by a human).
"""

from __future__ import annotations

import contextlib
import functools
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import registry
from app.db.transitions import StaleTransition, transition_row
from app.domain.states import (
    ENROLLMENT,
    DraftStatus,
    DraftType,
    EnrollmentState,
    Fit,
    OutreachMotion,
)
from app.integrations.llm.router import LLMRouter, LLMUnavailable
from app.jobs.worker import JobContext, PermanentJobError, RetryableJobError
from app.models import (
    Campaign,
    CampaignProspect,
    Company,
    EmailDraft,
    EmailDraftEvidence,
    Offering,
    Prospect,
    ResearchFact,
    ResearchFactEvidence,
    ResearchSource,
)
from app.services.campaigns.brief import CampaignBrief, OfferingDetails, is_ready
from app.services.campaigns.presets import PLAYBOOKS
from app.services.campaigns.service import DRAFT_JOB, brief_of, campaign_readiness, steps_of
from app.services.drafting.core import (
    REVIEWER_SYSTEM,
    WRITER_SYSTEM,
    DraftOutput,
    EvidenceItem,
    FactInput,
    Issue,
    OfferingInput,
    OfferingRefs,
    ProspectInput,
    ReviewOutput,
    assemble_body,
    content_hash,
    feedback_from,
    greeting_for,
    match_persona,
    offering_refs,
    rank_evidence,
    reviewer_prompt,
    score,
    tidy_sections,
    tidy_text,
    validate,
    word_count,
    writer_prompt,
)

log = get_logger(__name__)
drafts_total = Counter("drafts_total", "Draft generation outcomes", ["outcome"], registry=registry)


@dataclass
class DraftingServices:
    settings: Settings
    llm: LLMRouter


@dataclass
class DraftContext:
    enrollment_id: uuid.UUID
    campaign_id: uuid.UUID
    prospect_id: uuid.UUID
    workspace_id: uuid.UUID
    brief: CampaignBrief
    brief_version: int
    motion: str
    offering: OfferingInput
    prospect: ProspectInput
    facts: list[FactInput]
    step_ids: list[uuid.UUID]
    ready: bool
    readiness_blockers: list[str]
    current: dict[int, dict[str, Any]] = field(default_factory=dict)  # step -> current draft snapshot


def load_facts(db: Session, *, company_id: uuid.UUID | None, prospect_id: uuid.UUID) -> list[FactInput]:
    now = datetime.now(UTC)
    rows = db.execute(
        select(ResearchFact, func.min(ResearchFactEvidence.excerpt), func.min(ResearchSource.title))
        .join(ResearchFactEvidence, ResearchFactEvidence.fact_id == ResearchFact.id)
        .join(ResearchSource, ResearchSource.id == ResearchFactEvidence.source_id)
        .where(or_(ResearchFact.prospect_id == prospect_id,
                   ResearchFact.company_id == company_id if company_id else ResearchFact.id.is_(None)),
               or_(ResearchFact.expires_at.is_(None), ResearchFact.expires_at > now))
        .group_by(ResearchFact.id)).all()
    return [FactInput(fact_id=str(f.id), subject="person" if f.prospect_id else "company", category=f.category,
                      claim=f.claim, kind=f.kind.value, verification=f.verification_status.value,
                      confidence=float(f.confidence), relevance=float(f.relevance), excerpt=excerpt or "",
                      source_title=title, published_at=f.published_at) for f, excerpt, title in rows]


def load_context(db: Session, enrollment_id: uuid.UUID) -> DraftContext | None:
    cp = db.get(CampaignProspect, enrollment_id)
    if cp is None:
        return None
    campaign = db.get(Campaign, cp.campaign_id)
    prospect = db.get(Prospect, cp.prospect_id)
    if campaign is None or prospect is None:
        return None
    offering_row = db.get(Offering, campaign.offering_id) if campaign.offering_id else None
    company = db.get(Company, prospect.company_id) if prospect.company_id else None
    items = campaign_readiness(db, campaign)
    current = {}
    for d in db.scalars(select(EmailDraft).where(EmailDraft.campaign_id == campaign.id,
                                                 EmailDraft.prospect_id == prospect.id, EmailDraft.is_current)).all():
        current[d.step_order] = {"id": d.id, "subject": d.subject, "body": d.body_text, "sections": dict(d.sections),
                                 "status": d.status.value, "version": d.version}
    offering = OfferingInput(
        name=offering_row.name if offering_row else "", offering_type=offering_row.offering_type.value if offering_row
        else "other", category=offering_row.category if offering_row else None,
        summary=offering_row.summary if offering_row else "",
        details=OfferingDetails.model_validate(offering_row.details if offering_row else {}))
    return DraftContext(
        enrollment_id=cp.id, campaign_id=campaign.id, prospect_id=prospect.id, workspace_id=campaign.workspace_id,
        brief=brief_of(campaign), brief_version=campaign.brief_version, motion=campaign.motion.value,
        offering=offering,
        prospect=ProspectInput(first_name=prospect.first_name, full_name=prospect.full_name, title=prospect.title,
                               company_name=company.name if company else None, location=prospect.location),
        facts=load_facts(db, company_id=company.id if company else None, prospect_id=prospect.id),
        step_ids=[s.id for s in steps_of(db, campaign)], ready=is_ready(items),
        readiness_blockers=[i.message for i in items if i.severity == "blocker"], current=current)


@dataclass
class Attempt:
    output: DraftOutput
    subject: str
    sections: dict[str, str]
    body: str
    issues: list[Issue]
    review: ReviewOutput | None
    quality: dict[str, Any]
    evidence: list[EvidenceItem]
    refs: OfferingRefs
    attempts: int
    model: str
    tokens: int


@dataclass
class HeldBack:
    fit: Fit
    reason: str
    hits: list[str]
    persona: str | None


async def draft_step(svc: DraftingServices, c: DraftContext, step: int, *, force: bool,
                     keep_sections: dict[str, str] | None = None, rewrite: list[str] | None = None,
                     instruction: str | None = None) -> Attempt | HeldBack:
    brief = c.brief
    persona = match_persona(c.prospect.title, brief.audience.personas)
    evidence = rank_evidence(c.facts, brief, persona)
    if not evidence and not force:
        return HeldBack(Fit.WEAK, "No usable verified research evidence for this prospect.", [], None)
    refs = offering_refs(c.offering.details, brief.value_focus)
    greeting = greeting_for(brief, c.prospect)
    max_words = brief.style.max_words if step == 0 else max(50, int(brief.style.max_words * 0.7))
    previous = [(c.current[s]["subject"], c.current[s]["body"]) for s in sorted(c.current) if s < step]
    if step > 0 and not previous:
        raise PermanentJobError(f"Follow-up {step} needs the earlier email(s) drafted first.")
    playbook = PLAYBOOKS[OutreachMotion(c.motion)]
    feedback: list[str] = []
    best: Attempt | None = None
    total_tokens = 0
    for attempt in range(1, svc.settings.draft_max_attempts + 1):
        prompt = writer_prompt(brief=brief, playbook=playbook, offering=c.offering, refs=refs, persona=persona,
                               prospect=c.prospect, evidence=evidence, step=step, previous=previous, greeting=greeting,
                               max_words=max_words, feedback=feedback, keep_sections=keep_sections, rewrite=rewrite,
                               instruction=instruction)
        try:
            out, usage = await svc.llm.complete(task="write", system=WRITER_SYSTEM, user=prompt, output=DraftOutput,
                                                max_tokens=1800)
        except LLMUnavailable as exc:
            if best is not None:
                break  # keep the best earlier attempt rather than lose it
            raise RetryableJobError(f"Drafting models unavailable: {exc}", retry_after_seconds=exc.retry_after) from exc
        total_tokens += usage.prompt_tokens + usage.completion_tokens
        if step == 0 and attempt == 1 and not force and out.fit in (Fit.WEAK, Fit.DISQUALIFIED):
            drafts_total.labels("held_for_fit").inc()
            return HeldBack(out.fit, out.fit_reason, out.disqualifier_hits, out.persona)
        sections = tidy_sections(out.sections.model_dump())
        if keep_sections is not None and rewrite:
            sections = {k: (sections[k] if k in rewrite else keep_sections.get(k, sections[k])) for k in sections}
        subject = tidy_text(out.subject)
        if step > 0 and previous and not subject.lower().startswith("re:"):
            subject = f"Re: {previous[0][0]}"
        issues = validate(subject=subject, sections=sections, claims=out.claims, evidence=evidence, refs=refs,
                          brief=brief, offering=c.offering, prospect=c.prospect, max_words=max_words)
        body = assemble_body(greeting=greeting, sections=sections, brief=brief)
        review: ReviewOutput | None = None
        try:
            review, rusage = await svc.llm.complete(
                task="review", system=REVIEWER_SYSTEM, output=ReviewOutput, max_tokens=900,
                user=reviewer_prompt(subject=subject, body=body, evidence=evidence, offering=c.offering, refs=refs,
                                     brief=brief))
            total_tokens += rusage.prompt_tokens + rusage.completion_tokens
        except LLMUnavailable:
            issues.append(Issue("review_unavailable", "warning", "The AI review was unavailable for this attempt."))
        all_issues = [*issues]
        if review:
            all_issues += [Issue("unsupported_claim", "error", f"Reviewer: not supported by evidence: \"{u[:200]}\"",
                                 "review") for u in review.unsupported_claims]
            all_issues += [Issue("review_note", i.severity, i.message, "review") for i in review.issues]
        cited = [e for e in evidence if any(e.ref in cl.refs for cl in out.claims)]
        quality = score(issues=all_issues, review=review, evidence_used=cited, words=word_count(sections),
                        max_words=max_words, min_score=svc.settings.draft_min_score)
        candidate = Attempt(out, subject, sections, body, all_issues, review, quality, evidence, refs, attempt,
                            usage.model, total_tokens)
        if best is None or (quality["passed"], quality["overall"]) > (best.quality["passed"], best.quality["overall"]):
            best = candidate
        if quality["passed"]:
            break
        feedback = feedback_from(issues, review)
    assert best is not None
    best.attempts, best.tokens = attempt, total_tokens
    drafts_total.labels("passed" if best.quality["passed"] else "needs_attention").inc()
    return best


def save_draft(db: Session, c: DraftContext, step: int, a: Attempt, *, generated_by: str = "ai") -> uuid.UUID:
    """Persist a new current version for (campaign, prospect, step); the previous version is superseded."""
    previous = db.scalar(select(EmailDraft).where(EmailDraft.campaign_id == c.campaign_id,
                                                  EmailDraft.prospect_id == c.prospect_id,
                                                  EmailDraft.step_order == step, EmailDraft.is_current)
                         .with_for_update())
    last_version = db.scalar(select(func.max(EmailDraft.version)).where(
        EmailDraft.campaign_id == c.campaign_id, EmailDraft.prospect_id == c.prospect_id,
        EmailDraft.step_order == step)) or 0
    if previous is not None:
        previous.is_current = False
        previous.status = DraftStatus.SUPERSEDED
        db.flush()
    r = a.output.rationale
    draft = EmailDraft(
        workspace_id=c.workspace_id, campaign_id=c.campaign_id, prospect_id=c.prospect_id,
        sequence_step_id=c.step_ids[step] if step < len(c.step_ids) else None, step_order=step,
        draft_type=DraftType.INITIAL if step == 0 else DraftType.FOLLOW_UP, version=last_version + 1, is_current=True,
        subject=a.subject, body_text=a.body, content_sha256=content_hash(a.subject, a.body),
        personalization_rationale=f"Why them: {r.why_organization} Why this person: {r.why_person} "
                                  f"Why this offering: {r.why_offering} Why now: {r.why_now}",
        sections=a.sections,
        plan={"fit": a.output.fit.value, "fit_reason": a.output.fit_reason, "persona": a.output.persona,
              "opportunity": a.output.opportunity, "rationale": r.model_dump(),
              "disqualifier_hits": a.output.disqualifier_hits,
              "claims": [cl.model_dump() for cl in a.output.claims]},
        quality_scores=a.quality, validation_issues=[i.as_dict() for i in a.issues], generated_by=generated_by,
        generation_metadata={"model": a.model, "attempts": a.attempts, "tokens": a.tokens,
                             "brief_version": c.brief_version, "generated_at": datetime.now(UTC).isoformat(),
                             "evidence": [{"ref": e.ref, "fact_id": e.fact.fact_id} for e in a.evidence],
                             "offering_refs": {"items": a.refs.items,
                                               "proofs": {k: list(v) for k, v in a.refs.proofs.items()}}},
        status=DraftStatus.PENDING_REVIEW, overall_score=a.quality["overall"], fit=a.output.fit)
    db.add(draft)
    db.flush()
    fact_by_ref = {e.ref: e.fact.fact_id for e in a.evidence}
    seen: set[tuple[str, str]] = set()
    for claim in a.output.claims:
        for ref in claim.refs:
            fact_id = fact_by_ref.get(ref)
            if fact_id and (fact_id, claim.text) not in seen:
                seen.add((fact_id, claim.text))
                db.add(EmailDraftEvidence(draft_id=draft.id, fact_id=uuid.UUID(fact_id), claim_text=claim.text))
    db.flush()
    return draft.id


def _mark_awaiting_approval(db: Session, enrollment_id: uuid.UUID, fit: Fit | None, assessment: dict[str, Any] | None
                            ) -> None:
    cp = db.get(CampaignProspect, enrollment_id)
    if cp is None:
        return
    if fit is not None:
        cp.fit, cp.fit_assessment = fit, assessment
    cp.blocker = None
    if cp.state is EnrollmentState.AWAITING_DRAFT:
        with contextlib.suppress(StaleTransition):  # moved on concurrently (e.g. removed or replied)
            transition_row(db, CampaignProspect, cp.id, ENROLLMENT, EnrollmentState.AWAITING_APPROVAL,
                           expected_from=EnrollmentState.AWAITING_DRAFT)


def _hold(db: Session, enrollment_id: uuid.UUID, held: HeldBack) -> None:
    cp = db.get(CampaignProspect, enrollment_id)
    if cp is None:
        return
    cp.fit = held.fit
    cp.fit_assessment = {"fit": held.fit.value, "reason": held.reason, "disqualifier_hits": held.hits,
                         "persona": held.persona}
    cp.blocker = (f"Not drafted: fit assessed as {held.fit.value}. {held.reason} "
                  "Review it, and use \"Draft anyway\" if you still want to reach out.")[:1000]


def _services(ctx: JobContext) -> DraftingServices:
    svc = ctx.services.get("drafting")
    if svc is None:
        raise PermanentJobError("drafting services are not configured in this worker")
    return svc  # type: ignore[no-any-return]


async def handle_draft(ctx: JobContext) -> dict[str, Any]:
    svc = _services(ctx)
    payload = ctx.job.payload
    enrollment_id = uuid.UUID(payload["campaign_prospect_id"])
    c = await ctx.db(lambda s: load_context(s, enrollment_id))
    if c is None:
        raise PermanentJobError("enrollment no longer exists")

    async def set_blocker(message: str) -> None:
        def _do(s: Session) -> None:
            cp = s.get(CampaignProspect, enrollment_id)
            if cp is not None:
                cp.blocker = message[:1000]
        await ctx.db(_do)

    if not c.ready:
        await set_blocker("Campaign brief is incomplete: " + "; ".join(c.readiness_blockers))
        raise PermanentJobError("campaign brief is incomplete")
    if not svc.llm.configured("write"):
        await set_blocker("Drafting needs a Groq API key (GROQ_API_KEY). Add it and click Generate again.")
        raise PermanentJobError("no drafting model configured")

    steps = [int(s) for s in payload.get("steps") or [0]]
    force = bool(payload.get("force"))
    rewrite = [s for s in payload.get("sections") or [] if s in ("opening", "observation", "problem", "value", "cta")]
    results: dict[str, Any] = {}
    for step in steps:
        keep = c.current.get(step, {}).get("sections") if rewrite else None
        outcome = await draft_step(svc, c, step, force=force, keep_sections=keep, rewrite=rewrite or None,
                                   instruction=payload.get("instruction"))
        if isinstance(outcome, HeldBack):
            await ctx.db(functools.partial(_hold, enrollment_id=enrollment_id, held=outcome))
            results[str(step)] = {"held": outcome.fit.value}
            break
        draft_id = await ctx.db(functools.partial(save_draft, c=c, step=step, a=outcome))
        c.current[step] = {"id": draft_id, "subject": outcome.subject, "body": outcome.body,
                           "sections": outcome.sections, "status": "pending_review"}
        if step == 0:
            assessment = {"fit": outcome.output.fit.value, "reason": outcome.output.fit_reason,
                          "disqualifier_hits": outcome.output.disqualifier_hits, "persona": outcome.output.persona}
            await ctx.db(functools.partial(_mark_awaiting_approval, enrollment_id=enrollment_id,
                                           fit=outcome.output.fit, assessment=assessment))
        results[str(step)] = {"draft_id": str(draft_id), "passed": outcome.quality["passed"],
                              "score": outcome.quality["overall"], "attempts": outcome.attempts}
    return results


HANDLERS = {DRAFT_JOB: handle_draft}
