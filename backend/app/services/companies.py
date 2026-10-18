"""Company records: deduplicated by normalized domain within a workspace (race-safe upsert)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    def key(self) -> str | None:
        """Domain when known; otherwise the case-insensitive name."""
        return self.domain or (f"name:{company_name_key(self.name)}" if self.name else None)


@dataclass
class CompanyResolution:
    ids: dict[str, uuid.UUID]  # CompanyInput.key -> company id
    created: int
    reused: int


def resolve_companies(db: Session, workspace_id: uuid.UUID, inputs: list[CompanyInput]) -> CompanyResolution:
    """Get-or-create companies. Existing companies are never overwritten (first writer wins).

    Domain-keyed rows use INSERT ... ON CONFLICT DO NOTHING against the partial unique index, so
    concurrent imports cannot create duplicates. Domainless companies are matched on exact
    case-insensitive name; that match is not constraint-protected (documented limitation).
    """
    by_key: dict[str, CompanyInput] = {}
    for item in inputs:
        if item.key and item.key not in by_key:
            by_key[item.key] = item
    ids: dict[str, uuid.UUID] = {}
    created = 0

    domain_inputs = [i for i in by_key.values() if i.domain]
    if domain_inputs:
        inserted = db.execute(
            pg_insert(Company)
            .values([{
                "id": uuid.uuid4(), "workspace_id": workspace_id, "name": i.name or i.domain,
                "normalized_domain": i.domain, "website": i.website or f"https://{i.domain}", "industry": i.industry,
            } for i in domain_inputs])
            .on_conflict_do_nothing(index_elements=["workspace_id", "normalized_domain"],
                                    index_where=Company.normalized_domain.is_not(None))
            .returning(Company.id)
        ).all()
        created += len(inserted)
        rows = db.execute(select(Company.normalized_domain, Company.id).where(
            Company.workspace_id == workspace_id,
            Company.normalized_domain.in_([i.domain for i in domain_inputs]))).all()
        ids.update({domain: cid for domain, cid in rows if domain})

    name_inputs = [i for i in by_key.values() if not i.domain]
    if name_inputs:
        wanted = {company_name_key(i.name): i for i in name_inputs if i.name}
        existing = db.execute(select(func.lower(Company.name), Company.id).where(
            Company.workspace_id == workspace_id, Company.normalized_domain.is_(None),
            func.lower(Company.name).in_(list(wanted)))).all()
        found = {name: cid for name, cid in existing}
        for name, item in wanted.items():
            if name in found:
                ids[item.key] = found[name]  # type: ignore[index]
                continue
            company = Company(workspace_id=workspace_id, name=item.name or "", website=item.website,
                              industry=item.industry)
            db.add(company)
            db.flush()
            ids[item.key] = company.id  # type: ignore[index]
            created += 1

    return CompanyResolution(ids=ids, created=created, reused=len(ids) - created)
