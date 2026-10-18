import type { Tone } from "@/components/ui";
import type { ImportRowStatus, ImportStatus, OutreachStatus, ResearchStatus } from "@/lib/types";

export const RESEARCH_LABELS: Record<ResearchStatus, { label: string; tone: Tone }> = {
  pending: { label: "Pending", tone: "neutral" },
  researching_company: { label: "Researching company", tone: "info" },
  researching_individual: { label: "Researching individual", tone: "info" },
  verifying: { label: "Research verification", tone: "info" },
  ready_for_personalization: { label: "Ready for personalization", tone: "success" },
  needs_review: { label: "Needs review", tone: "warning" },
  insufficient_evidence: { label: "Insufficient evidence", tone: "warning" },
  failed_retryable: { label: "Failed (retryable)", tone: "danger" },
};

export const OUTREACH_LABELS: Record<OutreachStatus, { label: string; tone: Tone }> = {
  not_enrolled: { label: "Not enrolled", tone: "neutral" },
  enrolled: { label: "Enrolled", tone: "info" },
  suppressed: { label: "Suppressed", tone: "danger" },
};

export const IMPORT_STATUS_LABELS: Record<ImportStatus, { label: string; tone: Tone }> = {
  uploaded: { label: "Needs mapping", tone: "warning" },
  validated: { label: "Ready to import", tone: "info" },
  importing: { label: "Importing", tone: "info" },
  completed: { label: "Imported", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const ROW_STATUS_LABELS: Record<ImportRowStatus, { label: string; tone: Tone }> = {
  valid: { label: "Will import", tone: "success" },
  imported: { label: "Imported", tone: "success" },
  invalid: { label: "Invalid", tone: "danger" },
  duplicate_in_file: { label: "Duplicate in file", tone: "warning" },
  already_exists: { label: "Already exists", tone: "neutral" },
};

export const ISSUE_LABELS: Record<string, string> = {
  email_invalid: "Invalid email",
  email_multiple: "Several emails in one cell",
  email_freemail: "Personal email provider",
  email_role_account: "Role-based address",
  no_person: "No person (company-only row)",
  insufficient_identity: "Name without email or company",
  no_email: "No email address",
  no_name: "No name",
  no_company: "No company",
  website_invalid: "Invalid website",
  company_domain_invalid: "Invalid domain",
  linkedin_invalid: "Not a LinkedIn URL",
  linkedin_not_profile: "Not a personal LinkedIn profile",
  duplicate_in_file: "Duplicate email in file",
  already_exists: "Prospect already exists",
  possible_duplicate: "Possible duplicate (same name and company)",
  possible_existing_duplicate: "Possible duplicate of an existing prospect",
  extra_cells: "Extra cells ignored",
};

export function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
