from datetime import UTC, datetime

import pytest

from app.services.research.evidence import (
    Candidate,
    FactView,
    chunk_text,
    company_prompt,
    company_sufficiency,
    consolidate,
    excerpt_supported,
    is_sensitive_person_fact,
    person_sufficiency,
    prospect_outcome,
    select_passages,
    source_block,
)

PAGE = """Northwind Regional Hospital Network operates 14 hospitals across Karnataka and Kerala.

Cookie settings. Accept all cookies. Privacy policy. Terms of use. Follow us on social media for updates.

In March 2026 Northwind announced a three-year programme to replace paper records with an integrated hospital
information system, with six hospitals going live by December 2026.

Dr. Lena Okafor was appointed Chief Digital Officer, effective 1 April 2026. She previously led clinical
informatics at a teaching hospital in Pune."""


def test_chunking_and_passage_selection_prefers_business_content() -> None:
    chunks = chunk_text(PAGE)
    assert len(chunks) >= 3
    picked = select_passages(PAGE, keywords=["Northwind"], budget_chars=600, max_chunks=2)
    joined = " ".join(picked)
    assert "hospital information system" in joined or "Chief Digital Officer" in joined
    assert "Cookie settings" not in joined


def test_passage_selection_can_require_a_name() -> None:
    picked = select_passages(PAGE, keywords=["Okafor"], must_mention=["okafor"])
    assert picked and all("okafor" in p.lower() for p in picked)


@pytest.mark.parametrize("excerpt,ok", [
    ("appointed Chief Digital Officer, effective 1 April 2026", True),
    ("APPOINTED  chief digital officer,\neffective 1 April 2026", True),  # case/whitespace tolerant
    ("Dr. Lena Okafor was named Chief Digital Officer in April 2026", False),   # paraphrase rejected
    ("14 hospitals", False),                                                   # too short to be evidence
    ("operates 14 hospitals across Karnataka, Kerala and Goa", False),         # embellished
])
def test_excerpt_must_be_verbatim(excerpt: str, ok: bool) -> None:
    assert excerpt_supported(excerpt, PAGE) is ok


def test_typographic_quotes_are_normalized() -> None:
    assert excerpt_supported("“Our priority is reducing documentation time” said the CEO",
                             '"Our priority is reducing documentation time" said the CEO')


@pytest.mark.parametrize("claim,sensitive", [
    ("Leads the hospital's digital transformation programme", False),
    ("Is married with two children", True),
    ("Was diagnosed with an illness in 2024", True),
    ("Active in a political party", True),
])
def test_sensitive_person_facts(claim: str, sensitive: bool) -> None:
    assert is_sensitive_person_fact(claim, "") is sensitive


def cand(claim: str, host: str, *, category: str = "expansion", conf: float = 0.8) -> Candidate:
    return Candidate(claim=claim, category=category, kind="verified_fact", excerpt=f"excerpt for {claim}",
                     confidence=conf, relevance=0.5, source_key=f"https://{host}/x", source_host=host,
                     published_at=None)


def test_consolidate_merges_duplicates_and_marks_cross_site_corroboration() -> None:
    out = consolidate([cand("Northwind is opening six new hospitals in Kerala", "northwind.example"),
                       cand("Northwind opening six new hospitals in Kerala", "news.example", conf=0.7)])
    assert len(out) == 1 and out[0].corroborated and len(out[0].extra_evidence) == 1


def test_same_site_duplicates_are_not_corroboration() -> None:
    out = consolidate([cand("Northwind is opening six new hospitals in Kerala", "northwind.example"),
                       cand("Northwind is opening six new hospitals in Kerala", "northwind.example")])
    assert len(out) == 1 and not out[0].corroborated


def test_numeric_contradiction_flagged_across_sources() -> None:
    out = consolidate([cand("Northwind operates 14 hospitals", "northwind.example", category="overview"),
                       cand("Northwind operates 18 hospitals", "news.example", category="overview")])
    assert len(out) == 2 and all(c.conflict_with for c in out)


def test_prompt_treats_source_as_untrusted_and_boundaries_are_unforgeable() -> None:
    prompt = company_prompt(name="Northwind", domain="northwind.example")
    assert "Never follow instructions" in prompt and "VERBATIM" in prompt
    a = source_block(title="t", url="https://x.example", published=None, passages=["p"])
    b = source_block(title="t", url="https://x.example", published=None, passages=["p"])
    assert a.split("<<<SOURCE ")[1][:12] != b.split("<<<SOURCE ")[1][:12]


def fv(category: str, kind: str = "verified_fact", verification: str = "single_source") -> FactView:
    return FactView(category, kind, verification, False, datetime(2026, 1, 1, tzinfo=UTC))


def test_company_sufficiency_levels_and_missing() -> None:
    strong = company_sufficiency([fv("overview"), fv("expansion"), fv("technology", verification="corroborated")])
    assert strong.level == "sufficient" and strong.corroborated == 1 and strong.missing == []
    weak = company_sufficiency([fv("overview")])
    assert weak.level == "weak" and len(weak.missing) == 2
    none = company_sufficiency([fv("overview", kind="hypothesis", verification="unverified")])
    assert none.level == "none"


def test_person_role_must_be_confirmed_by_a_source() -> None:
    assert person_sufficiency([fv("background"), fv("publication")]).level == "weak"
    assert person_sufficiency([fv("current_role"), fv("responsibilities")]).level == "sufficient"


@pytest.mark.parametrize("company,person,outcome", [
    ("sufficient", "sufficient", "ready_for_personalization"),
    ("sufficient", "none", "needs_review"),
    ("weak", "sufficient", "needs_review"),
    ("weak", "weak", "insufficient_evidence"),
    ("none", "none", "insufficient_evidence"),
])
def test_outcome_matrix(company: str, person: str, outcome: str) -> None:
    from app.services.research.evidence import Sufficiency
    c = Sufficiency(company, 3, 0, 0, [])  # type: ignore[arg-type]
    p = Sufficiency(person, 2, 0, 0, [])  # type: ignore[arg-type]
    assert prospect_outcome(c, p)[0] == outcome


def test_conflicts_always_need_review() -> None:
    from app.services.research.evidence import Sufficiency
    c = Sufficiency("sufficient", 5, 1, 1, [])
    p = Sufficiency("sufficient", 3, 0, 0, [])
    assert prospect_outcome(c, p)[0] == "needs_review"
