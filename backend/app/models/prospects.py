"""Companies, prospects, and import provenance."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, WorkspaceScoped, enum_type
from app.domain.states import EmailVerificationStatus, ImportRowStatus, ImportStatus, ResearchStatus


class Company(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "companies"
    __table_args__ = (
        # Company-level research is reused across every prospect sharing a normalized domain.
        Index(
            "uq_companies_workspace_domain", "workspace_id", "normalized_domain",
            unique=True, postgresql_where=text("normalized_domain IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_domain: Mapped[str | None] = mapped_column(String(253))
    website: Mapped[str | None] = mapped_column(String(2048))
    industry: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(300))
    research_status: Mapped[ResearchStatus] = mapped_column(
        enum_type(ResearchStatus), nullable=False, default=ResearchStatus.PENDING
    )
    last_researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProspectImport(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "prospect_imports"

    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    encoding: Mapped[str] = mapped_column(String(20), nullable=False, default="utf-8")
    delimiter: Mapped[str] = mapped_column(String(4), nullable=False, default=",")
    # Header names exactly as parsed (de-duplicated), in file order.
    headers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    column_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    validation_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[ImportStatus] = mapped_column(
        enum_type(ImportStatus), nullable=False, default=ImportStatus.UPLOADED
    )
    error: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProspectImportRow(UUIDPrimaryKey, Base):
    """One uploaded data row: the original values (provenance), the normalized result, and why it was or
    was not imported. Kept after commit so every prospect can be traced back to its source cell values."""

    __tablename__ = "prospect_import_rows"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number"),
        Index("ix_prospect_import_rows_import_status", "import_id", "status"),
    )

    import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prospect_imports.id", ondelete="CASCADE"), nullable=False
    )
    # Spreadsheet-style row number: the header is row 1, the first data row is row 2.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issues: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[ImportRowStatus | None] = mapped_column(enum_type(ImportRowStatus))
    duplicate_of_row: Mapped[int | None] = mapped_column(Integer)
    existing_prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="SET NULL")
    )
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="SET NULL"), index=True
    )


class Prospect(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "prospects"
    __table_args__ = (
        # Identity scope: one person per normalized work email per workspace. Prospects without an
        # email are allowed (they can be researched) but can never be sent to; name matches are
        # never auto-merged.
        Index(
            "uq_prospects_workspace_email", "workspace_id", "email_normalized",
            unique=True, postgresql_where=text("email_normalized IS NOT NULL"),
        ),
        Index("ix_prospects_workspace_research_status", "workspace_id", "research_status"),
    )

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospect_imports.id", ondelete="SET NULL"), index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(150))
    last_name: Mapped[str | None] = mapped_column(String(150))
    full_name: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    email_normalized: Mapped[str | None] = mapped_column(String(320))
    email_verification_status: Mapped[EmailVerificationStatus] = mapped_column(
        enum_type(EmailVerificationStatus), nullable=False, default=EmailVerificationStatus.UNVERIFIED
    )
    title: Mapped[str | None] = mapped_column(String(300))
    linkedin_url: Mapped[str | None] = mapped_column(String(2048))
    industry: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(100))
    # Free-text location as supplied (e.g. "New Delhi, India"); never parsed into facts.
    location: Mapped[str | None] = mapped_column(String(300))
    timezone: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="csv_import")
    # Original uploaded row exactly as received, for provenance.
    source_row: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    research_status: Mapped[ResearchStatus] = mapped_column(
        enum_type(ResearchStatus), nullable=False, default=ResearchStatus.PENDING
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


