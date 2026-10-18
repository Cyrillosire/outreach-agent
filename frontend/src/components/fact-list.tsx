import { Badge, EmptyState, type Tone } from "@/components/ui";
import type { Fact, FactVerification } from "@/lib/types";

const VERIFICATION: Record<FactVerification, { label: string; tone: Tone; hint: string }> = {
  corroborated: { label: "Corroborated", tone: "success", hint: "Supported by two or more independent sites" },
  single_source: { label: "Single source", tone: "info", hint: "Stated by one source" },
  unverified: { label: "Unverified", tone: "neutral", hint: "Not stated as fact by the source" },
  contradicted: { label: "Conflicting", tone: "danger", hint: "Sources disagree; needs human review" },
  rejected: { label: "Rejected", tone: "danger", hint: "Failed verification" },
};

function label(category: string): string {
  return category.replaceAll("_", " ");
}

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Evidence-backed facts. Every fact links to the exact excerpt and source it came from. */
export function FactList({ facts, emptyTitle, emptyHint }: { facts: Fact[]; emptyTitle: string; emptyHint?: string }) {
  if (facts.length === 0) return <EmptyState title={emptyTitle}>{emptyHint}</EmptyState>;
  return (
    <ul className="divide-y divide-border">
      {facts.map((fact) => {
        const v = VERIFICATION[fact.verification_status];
        return (
          <li key={fact.id} className="py-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <p className="text-sm font-medium">{fact.claim}</p>
              <div className="flex shrink-0 flex-wrap gap-1">
                <Badge tone="neutral">{label(fact.category)}</Badge>
                {fact.kind === "hypothesis" ? <Badge tone="warning">Hypothesis</Badge> : null}
                <span title={v.hint}><Badge tone={v.tone}>{v.label}</Badge></span>
              </div>
            </div>
            {fact.conflict_note ? <p className="mt-1 text-xs text-danger">{fact.conflict_note}</p> : null}
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-muted">
                Evidence from {fact.evidence.length} source{fact.evidence.length === 1 ? "" : "s"} · confidence{" "}
                {Math.round(fact.confidence * 100)}%
                {fact.published_at ? ` · published ${new Date(fact.published_at).toLocaleDateString()}` : ""}
              </summary>
              <ul className="mt-2 space-y-2">
                {fact.evidence.map((ev, i) => (
                  <li key={`${ev.source.id}-${i}`} className="rounded-md bg-surface-muted p-2 text-xs">
                    <blockquote className="border-l-2 border-border pl-2 italic">&ldquo;{ev.excerpt}&rdquo;</blockquote>
                    <div className="mt-1 text-muted">
                      <a href={ev.source.url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent underline">
                        {ev.source.title || host(ev.source.url)}
                      </a>{" "}
                      · {host(ev.source.url)} · {ev.source.source_type.replaceAll("_", " ")} · accessed{" "}
                      {new Date(ev.source.accessed_at).toLocaleDateString()}
                    </div>
                  </li>
                ))}
              </ul>
            </details>
          </li>
        );
      })}
    </ul>
  );
}
