import type { Metadata } from "next";
import Link from "next/link";

import { ProspectForm } from "@/components/prospect-form";
import { Card, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Add prospect" };

export default function NewProspectPage() {
  return (
    <>
      <PageHeader
        title="Add prospect"
        description="Needs a work email, or a name plus a company. Values are validated with the same rules as CSV imports."
      />
      <p className="-mt-4 mb-6 text-sm"><Link href="/prospects" className="text-accent underline">← Prospects</Link></p>
      <Card className="max-w-3xl"><ProspectForm /></Card>
    </>
  );
}
