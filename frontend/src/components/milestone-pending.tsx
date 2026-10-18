import { PageHeader } from "@/components/ui";

/**
 * Honest placeholder for pages whose backend does not exist yet. It shows no controls, so nothing in the
 * UI appears operational before it is.
 */
export function MilestonePending({ title, milestone, summary, capabilities }: {
  title: string;
  milestone: number;
  summary: string;
  capabilities: string[];
}) {
  return (
    <>
      <PageHeader title={title} description={summary} />
      <div className="rounded-lg border border-dashed border-border bg-surface p-6">
        <div className="text-sm font-medium">Not built yet: scheduled for Milestone {milestone}</div>
        <p className="mt-1 text-sm text-muted">When this page is built, it will let you:</p>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted">
          {capabilities.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      </div>
    </>
  );
}
