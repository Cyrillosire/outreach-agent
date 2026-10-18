"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export interface NavItem {
  href: string;
  label: string;
  // Milestone in which the page becomes functional; undefined means available now.
  milestone?: number;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Overview" },
  { href: "/prospects", label: "Prospects" },
  { href: "/companies", label: "Companies" },
  { href: "/duplicates", label: "Duplicates" },
  { href: "/research", label: "Research" },
  { href: "/offerings", label: "Offerings" },
  { href: "/campaigns", label: "Campaigns" },
  { href: "/drafts", label: "Draft review" },
  { href: "/inbox", label: "Inbox & replies", milestone: 6 },
  { href: "/settings", label: "Settings" },
];

export function SidebarNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="flex flex-col gap-0.5">
      {NAV_ITEMS.map((item) => {
        const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`flex items-center justify-between rounded-md px-3 py-2 text-sm transition-colors ${
              active ? "bg-surface-muted font-medium text-foreground" : "text-muted hover:bg-surface-muted"
            }`}
          >
            <span>{item.label}</span>
            {item.milestone ? (
              <span className="text-[10px] font-medium uppercase tracking-wide text-muted" title="Not built yet">
                M{item.milestone}
              </span>
            ) : null}
          </Link>
        );
      })}
    </nav>
  );
}
