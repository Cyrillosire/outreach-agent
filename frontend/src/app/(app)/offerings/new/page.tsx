import type { Metadata } from "next";

import { PageHeader } from "@/components/ui";

import { OfferingForm } from "../offering-form";

export const metadata: Metadata = { title: "New offering" };

export default function NewOfferingPage() {
  return (
    <>
      <PageHeader title="New offering"
                  description="Describe what you're selling or proposing. Be specific: drafts can only use what's written here." />
      <OfferingForm />
    </>
  );
}
