import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { serverFetchOrNull } from "@/lib/api-server";
import type { ImportDetail } from "@/lib/types";

import { ImportWorkspace } from "./import-workspace";

export const metadata: Metadata = { title: "Review import" };

export default async function ImportReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();
  const detail = await serverFetchOrNull<ImportDetail>(`/imports/${id}`);
  if (!detail) notFound();
  return <ImportWorkspace initial={detail} />;
}
