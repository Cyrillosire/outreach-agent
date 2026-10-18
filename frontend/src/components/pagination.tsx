import Link from "next/link";

import { buttonClass } from "@/components/ui";

/** Server-rendered pagination using links, so it works without client JavaScript. */
export function Pagination({ page, pageSize, total, hrefFor }: {
  page: number;
  pageSize: number;
  total: number;
  hrefFor: (page: number) => string;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total === 0) return null;
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <nav aria-label="Pagination" className="mt-3 flex items-center justify-between text-sm text-muted">
      <span>{from}–{to} of {total}</span>
      <div className="flex gap-2">
        {page > 1 ? <Link className={buttonClass()} href={hrefFor(page - 1)}>Previous</Link> : null}
        {page < pages ? <Link className={buttonClass()} href={hrefFor(page + 1)}>Next</Link> : null}
      </div>
    </nav>
  );
}
