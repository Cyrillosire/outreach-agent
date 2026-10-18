import type { Tone } from "@/components/ui";
import type {
  CampaignStatus, CtaType, DraftStatus, EnrollmentState, Fit, ImportRowStatus, ImportStatus, OfferingType,
  OutreachMotion, OutreachStatus, ProofKind, ResearchStatus,
} from "@/lib/types";

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

// --- Offerings, campaigns, drafts ---------------------------------------------------------------

export const OFFERING_TYPE_LABELS: Record<OfferingType, string> = {
  product: "Product / software",
  service: "Service",
  partnership_program: "Partnership program",
  pilot_program: "Pilot program",
  event: "Event",
  research_collaboration: "Research collaboration",
  other: "Other",
};

export const MOTION_LABELS: Record<OutreachMotion, string> = {
  product_sale: "Product sale",
  partnership_proposal: "Partnership proposal",
  pilot_offer: "Pilot offer",
  service_engagement: "Service engagement",
  event_invitation: "Event invitation",
  research_or_feedback: "Research / feedback",
};

export const PROOF_KIND_LABELS: Record<ProofKind, string> = {
  metric: "Metric / result",
  case_study: "Case study",
  customer: "Named customer",
  certification: "Certification",
  award: "Award",
  other: "Other",
};

export const CTA_LABELS: Record<CtaType, string> = {
  book_meeting: "Book a meeting",
  reply_interest: "Reply if interested",
  request_pilot: "Start a pilot",
  share_materials: "Send materials",
  event_rsvp: "RSVP to an event",
  referral: "Refer the right person",
};

export const CAMPAIGN_STATUS_LABELS: Record<CampaignStatus, { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "neutral" },
  active: { label: "Active", tone: "success" },
  paused: { label: "Paused", tone: "warning" },
  completed: { label: "Completed", tone: "info" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const ENROLLMENT_LABELS: Record<EnrollmentState, { label: string; tone: Tone }> = {
  enrolled: { label: "Waiting", tone: "neutral" },
  awaiting_draft: { label: "Drafting", tone: "info" },
  awaiting_approval: { label: "Needs review", tone: "warning" },
  scheduled: { label: "Scheduled", tone: "info" },
  in_sequence: { label: "In sequence", tone: "info" },
  replied: { label: "Replied", tone: "success" },
  paused: { label: "Paused", tone: "warning" },
  completed: { label: "Completed", tone: "success" },
  suppressed: { label: "Suppressed", tone: "danger" },
  bounced: { label: "Bounced", tone: "danger" },
  unsubscribed: { label: "Unsubscribed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const DRAFT_STATUS_LABELS: Record<DraftStatus, { label: string; tone: Tone }> = {
  pending_review: { label: "Needs review", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  superseded: { label: "Superseded", tone: "neutral" },
};

export const FIT_LABELS: Record<Fit, { label: string; tone: Tone }> = {
  strong: { label: "Strong fit", tone: "success" },
  moderate: { label: "Moderate fit", tone: "info" },
  weak: { label: "Weak fit", tone: "warning" },
  disqualified: { label: "Disqualified", tone: "danger" },
};

export const SECTION_ORDER = ["opening", "observation", "problem", "value", "cta"] as const;
export const SECTION_LABELS: Record<string, string> = {
  opening: "Opening (research hook)",
  observation: "Observation",
  problem: "Problem / hypothesis",
  value: "Value",
  cta: "Call to action",
};

export const SCORE_LABELS: Record<string, string> = {
  evidence_strength: "Evidence strength",
  individual_relevance: "Relevance to the person",
  company_relevance: "Relevance to the company",
  offering_alignment: "Offering alignment",
  naturalness: "Natural tone",
  conciseness: "Conciseness",
  value_clarity: "Value clarity",
  cta_quality: "Call to action",
  claim_safety: "Claim safety",
  deliverability_hygiene: "Deliverability hygiene",
};

export function stepLabel(step: number): string {
  return step === 0 ? "First email" : `Follow-up ${step}`;
}

export function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? "—" : `${Math.round(score * 100)}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
