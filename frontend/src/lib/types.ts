// Mirrors backend Pydantic schemas (app/schemas, app/api/v1). Keep in sync until OpenAPI codegen is added.

export type UserRole = "admin" | "member";

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  workspace_id: string;
}

export type ResearchStatus =
  | "pending"
  | "researching_company"
  | "researching_individual"
  | "verifying"
  | "ready_for_personalization"
  | "needs_review"
  | "insufficient_evidence"
  | "failed_retryable";

export type EmailAccountStatus = "connected" | "needs_reauth" | "error" | "disconnected";

export interface EmailAccountSummary {
  id: string;
  provider: "gmail" | "microsoft";
  email_address: string;
  status: EmailAccountStatus;
  last_synced_at: string | null;
}

export interface DashboardSummary {
  prospects_total: number;
  research_by_status: Record<ResearchStatus, number>;
  drafts_awaiting_approval: number;
  emails_provider_accepted: number;
  replies_received: number;
  active_sequences: number;
  scheduled_actions_pending: number;
  actions_needing_attention: number;
  email_accounts: EmailAccountSummary[];
}

export interface SystemStatus {
  environment: string;
  database: boolean;
  redis: boolean;
  schema_revision: string | null;
  schema_head: string | null;
  schema_up_to_date: boolean;
  credential_encryption_configured: boolean;
}

export interface PageMeta {
  page: number;
  page_size: number;
  total: number;
}

export type ImportStatus = "uploaded" | "validated" | "importing" | "completed" | "failed" | "cancelled";
export type ImportRowStatus = "valid" | "invalid" | "duplicate_in_file" | "already_exists" | "imported";

export interface RowIssue {
  code: string;
  field: string;
  message: string;
  severity: "error" | "warning";
}

export interface ValidationSummary {
  total_rows?: number;
  valid?: number;
  invalid?: number;
  duplicate_in_file?: number;
  already_exists?: number;
  with_warnings?: number;
  without_email?: number;
  blank_rows_skipped?: number;
  issue_counts?: Record<string, number>;
  companies_total?: number;
  companies_new?: number;
  companies_existing?: number;
  imported?: number;
  companies_created?: number;
  companies_reused?: number;
}

export interface ImportSummary {
  id: string;
  original_filename: string;
  file_size_bytes: number;
  encoding: string;
  delimiter: string;
  headers: string[];
  row_count: number;
  column_mapping: Record<string, string>;
  validation_summary: ValidationSummary;
  status: ImportStatus;
  error: string | null;
  created_at: string;
  committed_at: string | null;
}

export interface ImportDetail extends ImportSummary {
  targets: { value: string; label: string }[];
  sample_rows: Record<string, string>[];
}

export interface ImportRow {
  row_number: number;
  raw: Record<string, string>;
  normalized: Record<string, unknown> | null;
  issues: RowIssue[];
  status: ImportRowStatus | null;
  duplicate_of_row: number | null;
  existing_prospect_id: string | null;
  prospect_id: string | null;
}

export type OutreachStatus = "not_enrolled" | "enrolled" | "suppressed";

export interface ProspectListItem {
  id: string;
  full_name: string | null;
  email: string | null;
  title: string | null;
  company_id: string | null;
  company_name: string | null;
  company_domain: string | null;
  research_status: ResearchStatus;
  outreach_status: OutreachStatus;
  has_email: boolean;
  import_id: string | null;
  last_activity_at: string | null;
  next_action_at: string | null;
  created_at: string;
}

export interface Prospect {
  id: string;
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;
  email: string | null;
  email_normalized: string | null;
  email_verification_status: string;
  title: string | null;
  linkedin_url: string | null;
  industry: string | null;
  country: string | null;
  location: string | null;
  attributes: Record<string, string>;
  research_status: ResearchStatus;
  last_activity_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CompanyBrief {
  id: string;
  name: string;
  normalized_domain: string | null;
  website: string | null;
  industry: string | null;
  research_status: ResearchStatus;
}

export interface Suppression {
  id: string;
  email_normalized: string | null;
  domain: string | null;
  scope: "workspace" | "campaign";
  campaign_id: string | null;
  reason: string;
  note: string | null;
  is_permanent: boolean;
  created_at: string;
}

export interface ProspectDetail extends Prospect {
  company: CompanyBrief | null;
  provenance: {
    source: string;
    import_id: string | null;
    import_filename: string | null;
    row_number: number | null;
    raw: Record<string, string> | null;
  };
  suppressions: Suppression[];
  blockers: { code: string; message: string }[];
  research_fact_count: number;
  drafts: { id: string; subject: string; status: string; version: number }[];
  messages: { id: string; direction: string; subject: string | null }[];
  scheduled_actions: { id: string; action_type: string; due_at: string; state: string }[];
  enrollments: { id: string; campaign_id: string; state: string }[];
}

export interface CompanyDetail extends CompanyBrief {
  description: string | null;
  location: string | null;
  created_at: string;
  last_researched_at: string | null;
  prospects: Prospect[];
  facts: Fact[];
}

// --- Research (Milestone 3) --------------------------------------------------------------------

export type FactKind = "verified_fact" | "inference" | "hypothesis" | "missing" | "conflicting";
export type FactVerification = "unverified" | "single_source" | "corroborated" | "contradicted" | "rejected";
export type RunStatus = "queued" | "running" | "succeeded" | "insufficient_evidence" | "failed_retryable" | "failed" | "cancelled";

export interface EvidenceSource {
  id: string;
  url: string;
  title: string | null;
  publisher: string | null;
  source_type: string;
  published_at: string | null;
  accessed_at: string;
}

export interface Fact {
  id: string;
  category: string;
  claim: string;
  kind: FactKind;
  verification_status: FactVerification;
  confidence: number;
  relevance: number;
  conflict_note: string | null;
  published_at: string | null;
  expires_at: string | null;
  evidence: { source: EvidenceSource; excerpt: string }[];
}

export interface Sufficiency {
  level?: "sufficient" | "weak" | "none";
  verified?: number;
  corroborated?: number;
  conflicts?: number;
  missing?: string[];
  notes?: string[];
  sources?: number;
  outcome?: string;
  reason?: string;
}

export interface ResearchRun {
  id: string;
  subject_type: "company" | "prospect";
  company_id: string | null;
  prospect_id: string | null;
  status: RunStatus;
  stage: string | null;
  attempt_count: number;
  sufficiency: Sufficiency | null;
  error: { message?: string; final?: boolean } | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface CompanyResearch {
  id: string;
  name: string;
  normalized_domain: string | null;
  research_status: ResearchStatus;
  last_researched_at: string | null;
  latest_run: ResearchRun | null;
  facts: Fact[];
}

export interface ProspectResearch {
  prospect_id: string;
  research_status: ResearchStatus;
  outcome_reason: string | null;
  individual_run: ResearchRun | null;
  individual_facts: Fact[];
  company: CompanyResearch | null;
  in_progress: boolean;
}

export interface ResearchOverview {
  prospects_by_status: Record<ResearchStatus, number>;
  jobs_by_state: Record<string, number>;
  providers: Record<"groq" | "ollama" | "tavily" | "brave", boolean>;
  llm_extraction_chain: string[];
  search_order: string[];
  auto_start: boolean;
  recent_runs: (ResearchRun & { subject_name: string | null })[];
}

// --- Records management -------------------------------------------------------------------------

export interface CompanyListItem extends CompanyBrief {
  contacts: number;
  location: string | null;
  last_researched_at: string | null;
  created_at: string;
}

export interface DuplicateCompanyGroup {
  key: string;
  domain_conflict: boolean;
  records: { id: string; name: string; normalized_domain: string | null; industry: string | null;
             research_status: ResearchStatus; contacts: number; created_at: string }[];
}

export interface DuplicateContactGroup {
  key: string;
  records: { id: string; full_name: string | null; email: string | null; title: string | null; company_id: string | null;
             company_name: string | null; research_status: ResearchStatus; created_at: string; source: string }[];
}

// --- Offerings, campaigns, drafts (M4) ----------------------------------------------------------

export type OfferingType = "product" | "service" | "partnership_program" | "pilot_program" | "event"
  | "research_collaboration" | "other";
export type OutreachMotion = "product_sale" | "partnership_proposal" | "pilot_offer" | "service_engagement"
  | "event_invitation" | "research_or_feedback";
export type ProofKind = "metric" | "case_study" | "customer" | "certification" | "award" | "other";
export type CtaType = "book_meeting" | "reply_interest" | "request_pilot" | "share_materials" | "event_rsvp" | "referral";
export type CampaignStatus = "draft" | "active" | "paused" | "completed" | "cancelled";
export type EnrollmentState = "enrolled" | "awaiting_draft" | "awaiting_approval" | "scheduled" | "in_sequence"
  | "replied" | "paused" | "completed" | "suppressed" | "bounced" | "unsubscribed" | "cancelled";
export type DraftStatus = "pending_review" | "approved" | "rejected" | "superseded";
export type Fit = "strong" | "moderate" | "weak" | "disqualified";

export interface ProofPoint {
  id?: string;
  claim: string;
  kind: ProofKind;
  source?: string | null;
  can_name_publicly: boolean;
}

export interface OfferingDetails {
  problems_solved: string[];
  capabilities: string[];
  differentiators: string[];
  proof_points: ProofPoint[];
  forbidden_claims: string[];
  internal_notes: string | null;
}

export interface Offering {
  id: string;
  name: string;
  offering_type: OfferingType;
  category: string | null;
  summary: string;
  details: OfferingDetails;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface Persona {
  name: string;
  title_keywords: string[];
  pains: string[];
  priorities: string[];
  angle: string | null;
}

export interface CampaignBrief {
  schema_version: 1;
  goal: string;
  why_now: string | null;
  audience: {
    industries: string[];
    organization_types: string[];
    regions: string[];
    size_notes: string | null;
    personas: Persona[];
  };
  fit_signals: string[];
  disqualifiers: string[];
  value_focus: string[];
  cta: { type: CtaType; details: string; link: string | null };
  style: {
    tone: "formal" | "professional" | "warm" | "direct";
    max_words: number;
    language: string;
    greeting: string;
    sign_off: string;
    banned_phrases: string[];
  };
  sender: { name: string; title: string; company: string; signature: string; compliance_footer: string };
  sequence: { draft_followups: "after_initial_approved" | "upfront" | "manual" };
  allow_company_only_personalization: boolean;
}

export interface ReadinessItem {
  field: string;
  message: string;
  severity: "blocker" | "recommended";
}

export interface Playbook {
  motion: OutreachMotion;
  label: string;
  description: string;
  guidance: string;
  default_brief: CampaignBrief;
  followup_days: number[];
}

export interface Campaign {
  id: string;
  name: string;
  status: CampaignStatus;
  motion: OutreachMotion;
  offering_id: string | null;
  offering_name: string | null;
  brief: CampaignBrief;
  brief_version: number;
  timezone: string;
  step_days: number[];
  readiness: ReadinessItem[];
  ready: boolean;
  enrollment_counts: Record<string, number>;
  draft_counts: Record<string, number>;
  created_at: string;
  updated_at: string;
}

export interface Enrollment {
  id: string;
  prospect_id: string;
  full_name: string | null;
  email: string | null;
  title: string | null;
  company_name: string | null;
  research_status: ResearchStatus;
  state: EnrollmentState;
  blocker: string | null;
  fit: Fit | null;
  fit_assessment: { reason?: string; disqualifier_hits?: string[] } | null;
  draft_id: string | null;
  draft_status: DraftStatus | null;
  draft_score: number | null;
  draft_issues: number;
  enrolled_at: string;
}

export interface DraftIssue {
  code: string;
  severity: "error" | "warning";
  message: string;
  source: string;
}

export interface DraftListItem {
  id: string;
  prospect_id: string;
  prospect_name: string | null;
  prospect_email: string | null;
  company_name: string | null;
  campaign_id: string;
  campaign_name: string;
  step_order: number;
  draft_type: string;
  version: number;
  subject: string;
  status: DraftStatus;
  overall_score: number | null;
  fit: Fit | null;
  has_errors: boolean;
  issue_count: number;
  created_at: string;
}

export interface DraftEvidence {
  ref: string | null;
  fact_id: string;
  claim: string;
  category: string;
  kind: string;
  verification_status: string;
  subject: "person" | "company";
  cited: boolean;
  sources: { url: string; title: string | null; publisher: string | null; excerpt: string;
             published_at: string | null }[];
}

export interface DraftDetail {
  id: string;
  prospect_id: string;
  prospect_name: string | null;
  prospect_title: string | null;
  company_name: string | null;
  campaign_id: string;
  campaign_name: string;
  step_order: number;
  total_steps: number;
  draft_type: string;
  version: number;
  is_current: boolean;
  subject: string;
  body_text: string;
  sections: Record<string, string>;
  content_sha256: string;
  plan: {
    fit?: Fit;
    fit_reason?: string;
    persona?: string | null;
    opportunity?: string;
    disqualifier_hits?: string[];
    rationale?: Record<string, string>;
    claims?: { text: string; refs: string[]; kind: string }[];
  };
  quality_scores: {
    overall?: number;
    passed?: boolean;
    reviewed?: boolean;
    review_summary?: string | null;
    word_count?: number;
    components?: Record<string, number>;
  };
  validation_issues: DraftIssue[];
  status: DraftStatus;
  approved_at: string | null;
  rejected_reason: string | null;
  generated_by: string;
  generation_metadata: { model?: string; attempts?: number; brief_version?: number; generated_at?: string };
  evidence: DraftEvidence[];
  versions: { id: string; version: number; status: DraftStatus; generated_by: string; overall_score: number | null;
              created_at: string; is_current: boolean }[];
  max_words: number;
  enrollment_state: EnrollmentState | null;
  created_at: string;
}
