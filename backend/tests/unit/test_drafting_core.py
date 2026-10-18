from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.states import OutreachMotion
from app.services.campaigns.brief import (
    CampaignBrief,
    OfferingDetails,
    Persona,
    ProofPoint,
    is_ready,
    readiness,
)
from app.services.campaigns.presets import PLAYBOOKS, default_brief
from app.services.drafting.core import (
    DraftClaim,
    FactInput,
    OfferingInput,
    ProspectInput,
    assemble_body,
    content_hash,
    greeting_for,
    match_persona,
    offering_refs,
    rank_evidence,
    score,
    tidy_sections,
    validate,
    writer_prompt,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def fact(i: int, claim: str, *, subject="company", category="technology", kind="verified_fact",
         verification="single_source", relevance=0.8) -> FactInput:
    return FactInput(fact_id=f"00000000-0000-0000-0000-00000000000{i}", subject=subject, category=category, claim=claim,
                     kind=kind, verification=verification, confidence=0.9, relevance=relevance,
                     excerpt=f"Source text: {claim}", source_title="Source",
                     published_at=datetime(2026, 3, 1, tzinfo=UTC))


BRIEF = CampaignBrief(
    goal="Book discovery calls with hospital IT leaders",
    fit_signals=["hospital information system rollout", "digital transformation"],
    disqualifiers=["already uses a competing scribe"],
    audience={"personas": [Persona(name="Hospital CIO", title_keywords=["CIO", "chief information", "digital"],
                                   pains=["documentation burden on clinicians"], priorities=["clinician adoption"])]},
    cta={"type": "book_meeting", "details": "a 20-minute call"},
    style={"max_words": 120, "banned_phrases": ["game-changer"]},
    sender={"name": "Asha Rao", "signature": "Head of Partnerships, Penthus", "compliance_footer":
            "Penthus Health Pvt Ltd, 12 MG Road, Bengaluru 560001. Reply 'unsubscribe' to opt out."},
)
OFFERING = OfferingInput(
    name="Penthus Clinical Documentation", offering_type="product", category="ambient AI scribe",
    summary="Drafts clinical notes from consultations inside the HIS.",
    details=OfferingDetails(problems_solved=["Clinicians lose hours to documentation"],
                            capabilities=["Drafts structured notes in under a minute"],
                            proof_points=[ProofPoint(id="pp_abc1", kind="metric",
                                                     claim="Cut documentation time by 40% in a 3-hospital pilot")],
                            forbidden_claims=["replaces doctors"]))
PROSPECT = ProspectInput(first_name=None, full_name="Dr. Lena Okafor", title="Chief Digital Officer",
                         company_name="Northwind Health", location="Bengaluru")
EVIDENCE = rank_evidence([fact(1, "Northwind is rolling out a hospital information system across six hospitals"),
                          fact(2, "Lena Okafor leads clinical informatics", subject="person", category="current_role")],
                         BRIEF, BRIEF.audience.personas[0], now=NOW)
REFS = offering_refs(OFFERING.details, BRIEF.value_focus)
GOOD_SECTIONS = {
    "opening": "Saw that Northwind is rolling out a hospital information system across six hospitals.",
    "observation": "Rollouts like that usually put new pressure on clinicians' time.",
    "problem": "Is documentation load something your team is trying to reduce as part of it?",
    "value": "Penthus drafts structured notes in under a minute; a 3-hospital pilot cut documentation time by 40%.",
    "cta": "Would a 20-minute call to see whether it fits your rollout be useful?",
}
GOOD_CLAIMS = [
    DraftClaim(text="Northwind is rolling out a hospital information system across six hospitals", refs=["F1"],
               kind="research"),
    DraftClaim(text="Is documentation load something your team is trying to reduce", refs=[], kind="hypothesis"),
    DraftClaim(text="drafts structured notes in under a minute", refs=["C2"], kind="offering"),
    DraftClaim(text="a 3-hospital pilot cut documentation time by 40%", refs=["P1"], kind="offering"),
]


def run_validate(sections=None, claims=None, subject="Northwind's HIS rollout"):  # type: ignore[no-untyped-def]
    return validate(subject=subject, sections=sections or GOOD_SECTIONS,
                    claims=claims if claims is not None else GOOD_CLAIMS,
                    evidence=EVIDENCE, refs=REFS, brief=BRIEF, offering=OFFERING, prospect=PROSPECT, max_words=120)


def codes(issues) -> set[str]:  # type: ignore[no-untyped-def]
    return {i.code for i in issues}


# --- Greeting, persona, evidence --------------------------------------------------------------------

@pytest.mark.parametrize("first,full,expected", [
    ("Lena", "Lena Okafor", "Hi Lena,"), (None, "Dr. Lena Okafor", "Hi Dr. Okafor,"),
    (None, "Mr. Arjun Mehta", "Hi Arjun,"), (None, None, "Hi there,"), (None, "Priya", "Hi Priya,"),
])
def test_greeting(first, full, expected) -> None:  # type: ignore[no-untyped-def]
    assert greeting_for(BRIEF, ProspectInput(first, full, None, None, None)) == expected


def test_persona_match_by_title_keywords() -> None:
    assert match_persona("Chief Digital Officer", BRIEF.audience.personas).name == "Hospital CIO"  # type: ignore[union-attr]
    assert match_persona("Head of Nursing", BRIEF.audience.personas) is None


def test_evidence_ranking_prefers_fit_signals_and_drops_conflicts() -> None:
    facts = [fact(1, "Company sponsors a local marathon", category="other", relevance=0.4),
             fact(2, "Company is rolling out a new hospital information system", relevance=0.7),
             fact(3, "Company has 14 hospitals", kind="conflicting", verification="contradicted")]
    ranked = rank_evidence(facts, BRIEF, None, now=NOW)
    assert [e.fact.fact_id[-1] for e in ranked] == ["2", "1"]
    assert ranked[0].ref == "F1"


# --- Validation -----------------------------------------------------------------------------------------

def test_good_email_has_no_errors() -> None:
    assert not [i for i in run_validate() if i.severity == "error"]


def test_unsupported_number_is_an_error() -> None:
    sections = {**GOOD_SECTIONS, "value": "Penthus cut documentation time by 70% for 12 hospitals."}
    issues = run_validate(sections=sections)
    assert "unsupported_number" in codes(issues)
    assert {i.message.split('"')[1] for i in issues if i.code == "unsupported_number"} == {"70%", "12"}


def test_hypothesis_must_be_hedged() -> None:
    sections = {**GOOD_SECTIONS, "problem": "Your clinicians are drowning in documentation."}
    claims = [*GOOD_CLAIMS[:1],
              DraftClaim(text="Your clinicians are drowning in documentation", refs=[], kind="hypothesis")]
    assert "hypothesis_stated_as_fact" in codes(run_validate(sections=sections, claims=claims))


def test_claims_must_cite_real_sources_of_the_right_kind() -> None:
    claims = [DraftClaim(text="Northwind is expanding", refs=["F9"], kind="research"),
              DraftClaim(text="drafts structured notes", refs=["F1"], kind="offering")]
    assert {"unknown_reference", "uncited_research_claim", "uncited_offering_claim", "generic_email"} <= codes(
        run_validate(claims=claims))


def test_forbidden_banned_link_and_length_rules() -> None:
    sections = {**GOOD_SECTIONS,
                "value": "It's a game-changer that replaces doctors; see https://evil.example/x " + "word " * 120}
    found = codes(run_validate(sections=sections))
    assert {"forbidden_claim", "banned_phrase", "unexpected_link", "too_long"} <= found


def test_hygiene_warnings() -> None:
    sections = {**GOOD_SECTIONS, "opening": "I hope this finds you well! Act now for a FREE trial.", "cta": "Thanks."}
    found = codes(run_validate(sections=sections))
    assert {"generic_phrase", "exclamation", "spam_term", "all_caps", "weak_cta"} <= found


# --- Scoring, assembly, prompts ---------------------------------------------------------------------

def test_score_gate() -> None:
    from app.services.drafting.core import ReviewOutput
    review = ReviewOutput.model_validate({"scores": {k: 5 for k in (
        "evidence_strength", "individual_relevance", "company_relevance", "offering_alignment", "naturalness",
        "conciseness", "value_clarity", "cta_quality")}, "summary": "Strong"})
    good = score(issues=run_validate(), review=review, evidence_used=EVIDENCE, words=80, max_words=120, min_score=0.7)
    assert good["passed"] and good["overall"] > 0.85
    bad = score(issues=run_validate(sections={**GOOD_SECTIONS, "value": "It cut time by 70%."}), review=review,
                evidence_used=EVIDENCE, words=80, max_words=120, min_score=0.7)
    assert not bad["passed"] and bad["components"]["claim_safety"] < 0.8
    unsupported = score(issues=[], review=review.model_copy(update={"unsupported_claims": ["x"]}),
                        evidence_used=EVIDENCE, words=80, max_words=120, min_score=0.7)
    assert not unsupported["passed"]


def test_assembly_adds_signature_and_footer_from_brief_only() -> None:
    body = assemble_body(greeting="Hi Dr. Okafor,", sections=GOOD_SECTIONS, brief=BRIEF)
    assert body.startswith("Hi Dr. Okafor,") and "Best regards,\nAsha Rao\nHead of Partnerships, Penthus" in body
    assert body.rstrip().endswith("Reply 'unsubscribe' to opt out.")
    assert content_hash("s", body) != content_hash("s", body + " ") and len(content_hash("s", body)) == 64


def test_writer_prompt_contains_rules_material_and_untrusted_boundary() -> None:
    prompt = writer_prompt(brief=BRIEF, playbook=PLAYBOOKS[OutreachMotion.PRODUCT_SALE], offering=OFFERING, refs=REFS,
                           persona=BRIEF.audience.personas[0], prospect=PROSPECT, evidence=EVIDENCE, step=0,
                           previous=[],
                           greeting="Hi Dr. Okafor,", max_words=120, feedback=["The number \"70%\" isn't supported."])
    assert "F1 [company; technology" in prompt and "P1: PROOF:" in prompt and "FORBIDDEN CLAIMS" in prompt
    assert "<<<RESEARCH " in prompt and "YOUR PREVIOUS ATTEMPT FAILED REVIEW" in prompt
    assert "a 20-minute call" in prompt


# --- Brief, offering, playbooks -------------------------------------------------------------------

def test_readiness_blockers_and_recommendations() -> None:
    empty = readiness(CampaignBrief(), None, offering_set=False, steps=1)
    assert not is_ready(empty)
    assert {i.field for i in empty if i.severity == "blocker"} >= {"offering", "goal", "audience.personas",
                                                                    "sender.compliance_footer"}
    full = readiness(BRIEF, OFFERING.details, offering_set=True, steps=4)
    assert is_ready(full) and full == []


def test_offering_details_validation() -> None:
    details = OfferingDetails(capabilities=["  A  ", "A", "B"])
    assert details.capabilities == ["A", "B"]
    with pytest.raises(ValidationError, match="unique"):
        OfferingDetails(proof_points=[ProofPoint(id="pp_aaaa", claim="Claim one"),
                                      ProofPoint(id="pp_aaaa", claim="Claim two")])


@pytest.mark.parametrize("motion", list(OutreachMotion))
def test_every_motion_has_a_playbook_and_valid_default_brief(motion: OutreachMotion) -> None:
    brief = default_brief(motion)
    assert PLAYBOOKS[motion].guidance and brief.cta.details and 50 <= brief.style.max_words <= 250


def test_tidy_sections_drops_duplicate_greeting_and_odd_typography() -> None:
    out = tidy_sections({"opening": "Hi Lena,  saw the rollout.", "value": "a three\u2011hospital\u202fpilot"})
    assert out == {"opening": "saw the rollout.", "value": "a three-hospital pilot"}
    assert tidy_sections({"opening": "Hiring is up at Northwind."})["opening"] == "Hiring is up at Northwind."


def test_tidy_sections_strips_leaked_citation_markers() -> None:
    out = tidy_sections({"value": "It drafts notes in under a minute (C2) and cut time by 40% [P1, F2]."})
    assert out["value"] == "It drafts notes in under a minute and cut time by 40%."
    assert tidy_sections({"value": "Plan C (phase 2) stays."})["value"] == "Plan C (phase 2) stays."
