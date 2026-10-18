import { LogoutButton } from "@/components/logout-button";
import { SidebarNav } from "@/components/sidebar-nav";
import { serverFetch } from "@/lib/api-server";
import type { User } from "@/lib/types";

// Authoritative auth gate: the backend validates the session on every render of an app page.
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const user = await serverFetch<User>("/auth/me");

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="flex shrink-0 flex-col border-b border-border bg-surface md:w-60 md:border-b-0 md:border-r">
        <div className="px-5 py-4">
          <div className="text-sm font-semibold">Outreach Agent</div>
          <div className="mt-0.5 truncate text-xs text-muted" title={user.email}>{user.email}</div>
        </div>
        <div className="flex-1 px-2">
          <SidebarNav />
        </div>
        <div className="border-t border-border p-2">
          <LogoutButton />
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8">{children}</main>
    </div>
  );
}
