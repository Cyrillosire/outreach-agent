import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Badge, PageHeader } from "@/components/ui";
import { serverFetchOrNull } from "@/lib/api-server";
import { OFFERING_TYPE_LABELS } from "@/lib/labels";
import type { Offering } from "@/lib/types";

import { OfferingForm } from "../offering-form";

export const metadata: Metadata = { title: "Offering" };

export default async function OfferingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const offering = await serverFetchOrNull<Offering>(`/offerings/${id}`);
  if (!offering) notFound();
  return (
    <>
      <PageHeader title={offering.name} description={OFFERING_TYPE_LABELS[offering.offering_type]}
                  actions={offering.archived ? <Badge>Archived</Badge> : undefined} />
      <OfferingForm offering={offering} />
    </>
  );
}
