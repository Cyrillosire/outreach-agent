import type { Metadata } from "next";

import { MilestonePending } from "@/components/milestone-pending";

export const metadata: Metadata = { title: "Campaigns" };

export default function CampaignsPage() {
  return (
    <MilestonePending
      title="Campaigns" milestone={6}
      summary="Configure campaign goals, sequences, and sending policy."
      capabilities={[
        "Define the offering, target persona, value proposition, and tone",
        "Configure sequence steps, intervals, sending windows, time zone, and daily limits",
        "Choose the sending mailbox and the approval policy for follow-ups",
        "Pause, resume, or cancel a campaign safely",
      ]}
    />
  );
}
