import type { Metadata } from "next";

import { PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import type { Offering, Playbook } from "@/lib/types";

import { NewCampaignForm } from "./new-campaign-form";

export const metadata: Metadata = { title: "New campaign" };

export default async function NewCampaignPage() {
  const [playbooks, offerings] = await Promise.all([
    serverFetch<Playbook[]>("/campaigns/playbooks"),
    serverFetch<Offering[]>("/offerings"),
  ]);
  return (
    <>
      <PageHeader title="New campaign"
                  description="Choose how you're approaching these contacts. The playbook pre-fills a sensible brief, call to action, and follow-up schedule, which you can then tailor." />
      <NewCampaignForm playbooks={playbooks} offerings={offerings} />
    </>
  );
}
