"use client";

import type { ReactNode } from "react";

import { buttonClass } from "@/components/ui";

/** Sticky action bar shown while records are selected. */
export function BulkBar({ count, onClear, children }: { count: number; onClear: () => void; children: ReactNode }) {
  if (count === 0) return null;
  return (
    <div className="sticky top-2 z-10 mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-accent/40 bg-surface px-3 py-2 shadow-sm" role="region" aria-label="Bulk actions">
      <span className="text-sm font-medium">{count} selected</span>
      <button type="button" onClick={onClear} className={buttonClass("ghost")}>Clear</button>
      <div className="ml-auto flex flex-wrap gap-2">{children}</div>
    </div>
  );
}

export function useSelectionHelpers<T extends { id: string }>(rows: T[], selected: Set<string>,
                                                               setSelected: (s: Set<string>) => void) {
  const allOnPage = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };
  const toggleAll = () => {
    const next = new Set(selected);
    if (allOnPage) rows.forEach((r) => next.delete(r.id));
    else rows.forEach((r) => next.add(r.id));
    setSelected(next);
  };
  return { allOnPage, toggle, toggleAll };
}
