import type { Metadata } from "next";

import { MilestonePending } from "@/components/milestone-pending";

export const metadata: Metadata = { title: "Inbox & replies" };

export default function InboxPage() {
  return (
    <MilestonePending
      title="Inbox & replies" milestone={6}
      summary="Prospect replies and their linked conversations."
      capabilities={[
        "See every reply matched to its prospect and original thread",
        "Confirm that follow-ups stop automatically as soon as someone replies",
        "Review reply classifications (interested, referral, out of office, and so on)",
        "Decide explicitly whether to resume outreach after a reply",
      ]}
    />
  );
}
