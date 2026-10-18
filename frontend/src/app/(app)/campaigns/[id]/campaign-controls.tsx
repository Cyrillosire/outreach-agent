"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Modal } from "@/components/modal";
import { buttonClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import type { Campaign } from "@/lib/types";

const GENERATE_LABELS: Record<string, string> = {
  drafting_queued: "drafting started",
  waiting_for_research: "waiting for research",
  research_not_ready: "research not ready",
  held_for_review: "held (weak fit, use “Draft anyway”)",
  brief_incomplete: "brief incomplete",
};

export function CampaignControls({ campaign }: { campaign: Campaign }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "success" | "danger" | "info"; text: string } | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);

  async function act(path: string, done: (data: unknown) => string, body: unknown = {}) {
    setBusy(true);
    setMessage(null);
    try {
      const data = await apiFetch<unknown>(`/campaigns/${campaign.id}/${path}`, { method: "POST", body: JSON.stringify(body) });
      setMessage({ tone: "success", text: done(data) });
      setConfirmCancel(false);
      router.refresh();
    } catch (err) {
      setMessage({ tone: "danger", text: errorMessage(err, "That didn't work.") });
    } finally {
      setBusy(false);
    }
  }

  const generate = () => act("generate", (data) => {
    const entries = Object.entries(data as Record<string, number>);
    if (entries.length === 0) return "No contacts are waiting for a draft.";
    return entries.map(([k, n]) => `${n} ${GENERATE_LABELS[k] ?? k}`).join(" · ");
  });
  const live = campaign.status === "draft" || campaign.status === "active" || campaign.status === "paused";

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex flex-wrap gap-2">
        {live ? (
          <button type="button" className={buttonClass("primary")} disabled={busy || !campaign.ready} onClick={generate}
                  title={campaign.ready ? "Draft emails for contacts whose research is ready" : "Complete the brief first"}>
            Generate drafts
          </button>
        ) : null}
        {campaign.status === "draft" ? (
          <button type="button" className={buttonClass()} disabled={busy} onClick={() => act("activate", () => "Campaign activated.")}>
            Activate
          </button>
        ) : null}
        {campaign.status === "active" ? (
          <button type="button" className={buttonClass()} disabled={busy} onClick={() => act("pause", () => "Paused.")}>Pause</button>
        ) : null}
        {campaign.status === "paused" ? (
          <button type="button" className={buttonClass()} disabled={busy} onClick={() => act("resume", () => "Resumed.")}>Resume</button>
        ) : null}
        {live ? (
          <button type="button" className={buttonClass("ghost")} onClick={() => setConfirmCancel(true)}>Cancel campaign</button>
        ) : null}
      </div>
      {message ? <div className="max-w-md"><Notice tone={message.tone} title={message.text} /></div> : null}

      <Modal open={confirmCancel} title="Cancel this campaign?" onClose={() => setConfirmCancel(false)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setConfirmCancel(false)}>Keep it</button>
               <button type="button" className={buttonClass("danger")} disabled={busy}
                       onClick={() => act("cancel", () => "Campaign cancelled.")}>Cancel campaign</button>
             </>}>
        <p className="text-sm">All contacts stop at their current step and nothing further is drafted or sent. This can&apos;t be undone.</p>
      </Modal>
    </div>
  );
}
