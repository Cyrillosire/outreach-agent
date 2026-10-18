import type { Metadata } from "next";
import Link from "next/link";

import { Pagination } from "@/components/pagination";
import { buttonClass, EmptyState, inputClass, PageHeader } from "@/components/ui";
import { serverFetchPage } from "@/lib/api-server";
import { buildQuery, firstParam, pageParam, type SearchParams } from "@/lib/query";
import type { CompanyListItem, PageMeta } from "@/lib/types";

import { CompanyTable } from "./company-table";

export const metadata: Metadata = { title: "Companies" };

const KEYS = ["q", "has_domain", "sort"];

export default async function CompaniesPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const page = pageParam(params.page);
  const { data, meta } = await serverFetchPage<CompanyListItem[], PageMeta>(
    `/companies${buildQuery(params, [...KEYS, "page", "page_size"], { page: String(page), page_size: "50" })}`);
  const filtered = Boolean(firstParam(params.q) || firstParam(params.has_domain));

  return (
    <>
      <PageHeader
        title="Companies"
        description="One record per organization, matched by domain and exact name. Company research is shared by everyone who works there."
        actions={<Link href="/duplicates" className={buttonClass("ghost")}>Review duplicates</Link>}
      />
      <form method="get" className="mb-4 grid gap-2 rounded-lg border border-border bg-surface p-3 sm:grid-cols-4">
        <input name="q" defaultValue={firstParam(params.q) ?? ""} placeholder="Search name or domain…" aria-label="Search"
               className={`${inputClass} sm:col-span-2`} />
        <select name="has_domain" defaultValue={firstParam(params.has_domain) ?? ""} aria-label="Domain" className={inputClass}>
          <option value="">With or without domain</option>
          <option value="true">Has domain</option>
          <option value="false">No domain</option>
        </select>
        <div className="flex gap-2">
          <select name="sort" defaultValue={firstParam(params.sort) ?? "name"} aria-label="Sort" className={inputClass}>
            <option value="name">Name A–Z</option>
            <option value="-contacts">Most contacts</option>
            <option value="-created">Newest</option>
          </select>
          <button type="submit" className={buttonClass("primary")}>Apply</button>
        </div>
      </form>
      <CompanyTable rows={data} empty={
        <div className="p-4"><EmptyState title={filtered ? "No companies match" : "No companies yet"}>
          {filtered ? "Try a broader search." : "Companies are created automatically when you import or add contacts."}
        </EmptyState></div>
      } />
      <Pagination page={meta.page} pageSize={meta.page_size} total={meta.total}
                  hrefFor={(n) => `/companies${buildQuery(params, [...KEYS, "page"], { page: String(n) })}`} />
    </>
  );
}
