import type { Metadata } from "next";

import { MilestonePending } from "@/components/milestone-pending";

export const metadata: Metadata = { title: "Draft review" };

export default function DraftsPage() {
  return (
    <MilestonePending
      title="Draft review" milestone={4}
      summary="Review, edit, and approve personalized drafts before anything is sent."
      capabilities={[
        "Read each draft alongside the evidence behind every factual claim",
        "See quality scores, the personalization rationale, and any validation issues",
        "Edit or regenerate sections. Any change invalidates a previous approval",
        "Approve or reject drafts one at a time, or in bulk after review",
      ]}
    />
  );
}
