import type { Metadata } from "next";

import { MilestonePending } from "@/components/milestone-pending";

export const metadata: Metadata = { title: "Research" };

export default function ResearchPage() {
  return (
    <MilestonePending
      title="Research workspace" milestone={3}
      summary="Evidence-backed company and individual research for every prospect."
      capabilities={[
        "Track research progress per prospect and per company, shared across colleagues at one company",
        "Inspect each fact with its source URL, excerpt, publication date, and confidence",
        "See facts marked verified, inferred, hypothetical, conflicting, or missing",
        "Retry failed research and review prospects with insufficient evidence",
      ]}
    />
  );
}
