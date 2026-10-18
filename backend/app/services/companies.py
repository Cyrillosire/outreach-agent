"""Company records: deduplicated by normalized domain AND by exact name within a workspace.

Matching rules (in order) for an incoming company (name N, optional domain D):
1. An existing company with domain D is the same company.
2. Otherwise an existing company with exactly the same name (case/whitespace-insensitive) is the same company,
   unless both have *different* domains: then they are kept apart and surfaced on the duplicates page for a
   human decision, because merging two organizations that share a name would mix their research.
   A name match that has no domain yet adopts D.
3. Otherwise a new company is created.
Existing companies are never overwritten except to fill a missing domain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company
from app.services.imports.normalize import company_name_key


@dataclass(frozen=True)
class CompanyInput:
    name: str | None
    domain: str | None
    website: str | None = None
    industry: str | None = None

    @property
    def name_key(self) -> str | None:
        return company_name_key(self.name)

    @property
    def key(self) -> str | None:
        """Lookup key for callers: domain when known; otherwise the case-insensitive name."""
        return self.domain or (f"name:{self.name_key}" if self.name_key else None)


@dataclass
class CompanyResolution:
    ids: dict[str, uuid.UUID]  # CompanyInput.key -> company id
    created: int
    reused: int


def _existing_by_name(db: Session, workspace_id: uuid.UUID, name_keys: list[str]) -> dict[str, list[Company]]:
    if not name_keys:
        return {}
    rows = db.scalars(select(Company).where(
        Company.workspace_id == workspace_id,
        func.lower(func.regexp_replace(func.trim(Company.name), r"\s+", " ", "g")).in_(name_keys))
        .order_by(Company.created_at)).all()
    out: dict[str, list[Company]] = {}
    for c in rows:
        out.setdefault(company_name_key(c.name) or "", []).append(c)
    return out


def unify_batch(inputs: list[CompanyInput]) -> dict[str, CompanyInput]:
    """Collapse the batch: same domain = same company; a domainless input joins the single domain its name uses."""
    by_key: dict[str, CompanyInput] = {}
    domains_for_name: dict[str, set[str]] = {}
    for item in inputs:
        if item.domain and item.name_key:
            domains_for_name.setdefault(item.name_key, set()).add(item.domain)
    for item in inputs:
        if not item.key:
            continue
        if not item.domain and item.name_key and len(domains_for_name.get(item.name_key, set())) == 1:
            continue  # resolved through its name's single domain (alias added below)
        by_key.setdefault(item.key, item)
    return by_key


def plan_companies(db: Session, workspace_id: uuid.UUID, inputs: list[CompanyInput]) -> dict[str, uuid.UUID | None]:
    """Dry run: map each input key to an existing company id (or None if a new company would be created)."""
    unified = unify_batch(inputs)
    domains = [i.domain for i in unified.values() if i.domain]
    by_domain = dict(db.execute(select(Company.normalized_domain, Company.id).where(
        Company.workspace_id == workspace_id, Company.normalized_domain.in_(domains))).all()) if domains else {}
    by_name = _existing_by_name(db, workspace_id, sorted({i.name_key for i in unified.values() if i.name_key}))
    plan: dict[str, uuid.UUID | None] = {}
    for key, item in unified.items():
        match: uuid.UUID | None = by_domain.get(item.domain) if item.domain else None
        if match is None and item.name_key:
            for cand in by_name.get(item.name_key, []):
                if not item.domain or not cand.normalized_domain or cand.normalized_domain == item.domain:
                    match = cand.id
                    break
        plan[key] = match
    _add_name_aliases(inputs, plan)
    return plan


def _add_name_aliases(inputs: list[CompanyInput], ids: dict[str, uuid.UUID | None]) -> None:
    """Domainless inputs whose name maps to exactly one domain in the batch resolve to that domain's company."""
    domains_for_name: dict[str, set[str]] = {}
    for item in inputs:
        if item.domain and item.name_key:
            domains_for_name.setdefault(item.name_key, set()).add(item.domain)
    for item in inputs:
        if item.key and not item.domain and item.name_key:
            only = domains_for_name.get(item.name_key, set())
            if len(only) == 1:
                ids[item.key] = ids.get(next(iter(only)))


def resolve_companies(db: Session, workspace_id: uuid.UUID, inputs: list[CompanyInput]) -> CompanyResolution:
    """Get-or-create companies with the matching rules above. Race-safe for domains (unique index)."""
    unified = unify_batch(inputs)
    plan = plan_companies(db, workspace_id, list(unified.values()))
    ids: dict[str, uuid.UUID] = {}
    created = 0
    for key, item in unified.items():
        existing = plan.get(key)
        if existing is not None:
            ids[key] = existing
            if item.domain:  # an exact-name match without a domain adopts this one
                try:
                    with db.begin_nested():
                        db.execute(update(Company).where(Company.id == existing, Company.normalized_domain.is_(None))
                                   .values(normalized_domain=item.domain,
                                           website=func.coalesce(Company.website, item.website or f"https://{item.domain}")))
                except IntegrityError:
                    pass  # another company took the domain concurrently; keep the name match as-is
            continue
        if item.domain:
            new_id = db.execute(
                pg_insert(Company).values(id=uuid.uuid4(), workspace_id=workspace_id, name=item.name or item.domain,
                                          normalized_domain=item.domain, website=item.website or f"https://{item.domain}",
                                          industry=item.industry)
                .on_conflict_do_nothing(index_elements=["workspace_id", "normalized_domain"],
                                        index_where=Company.normalized_domain.is_not(None))
                .returning(Company.id)).scalar_one_or_none()
            if new_id is None:  # created concurrently by another import
                new_id = db.scalar(select(Company.id).where(Company.workspace_id == workspace_id,
                                                            Company.normalized_domain == item.domain))
            else:
                created += 1
            assert new_id is not None
            ids[key] = new_id
        else:
            company = Company(workspace_id=workspace_id, name=item.name or "", website=item.website,
                              industry=item.industry)
            db.add(company)
            db.flush()
            ids[key] = company.id
            created += 1
    aliases: dict[str, uuid.UUID | None] = dict(ids)
    _add_name_aliases(inputs, aliases)
    ids.update({k: v for k, v in aliases.items() if v is not None})
    return CompanyResolution(ids=ids, created=created, reused=len(unified) - created)
