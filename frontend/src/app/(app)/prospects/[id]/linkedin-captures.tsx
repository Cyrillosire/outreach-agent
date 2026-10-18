"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import type { LinkedInCapture } from "@/lib/types";

type Kind = LinkedInCapture["kind"];

const KINDS: Record<Kind, { label: string; path: string; hint: string }> = {
  activity: {
    label: "Activity (posts, comments, reposts, reactions)", path: "recent-activity/all/",
    hint: "Best personalization source. Open their Activity tab (All, or Comments / Reactions), scroll to load what you want included, then copy.",
  },
  profile: { label: "Profile (headline, about, experience)", path: "", hint: "Their main profile page." },
  post: { label: "A post or article they wrote", path: "recent-activity/shares/", hint: "Copy the post text." },
};

/**
 * Captures of the prospect's own LinkedIn pages, copied by the user from their normal browser session. The app never
 * signs in to or fetches LinkedIn itself; this keeps activity data (comments, reposts, reactions) available without
 * automating anyone's account.
 */
export function LinkedInCaptures({ prospectId, linkedinUrl, captures }: {
  prospectId: string;
  linkedinUrl: string | null;
  captures: LinkedInCapture[] | null;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("activity");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "danger" | "warning"; text: string } | null>(null);
  const base = linkedinUrl ? linkedinUrl.replace(/\/+$/, "") + "/" : null;
  const pageUrl = base ? base + KINDS[kind].path : null;

  async function save() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await apiFetch<{ already_running: boolean }>(`/prospects/${prospectId}/linkedin-captures`, {
        method: "POST", body: JSON.stringify({ kind, text, url: pageUrl }),
      });
      setText("");
      setOpen(false);
      setNotice(r.already_running
        ? { tone: "warning", text: "Saved. Research is already running; click “Refresh research” when it finishes to include this capture." }
        : { tone: "success", text: "Saved. Research is refreshing to pick up the new evidence." });
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't save the capture.") });
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setBusy(true);
    try {
      const r = await apiFetch<{ facts_removed: number }>(`/prospects/${prospectId}/linkedin-captures/${id}`,
                                                          { method: "DELETE" });
      setNotice({ tone: "success", text: `Capture removed (${r.facts_removed} fact(s) that relied on it).` });
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't remove the capture.") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="LinkedIn">
      <p className="mb-3 text-sm text-muted">
        Their profile headline and indexed posts are found automatically from the LinkedIn URL on file. For comments,
        reposts, and reactions, copy their activity page from your browser and paste it here. It&apos;s saved as
        first-party evidence about this person.
      </p>
      {notice ? <div className="mb-3"><Notice tone={notice.tone} title={notice.text} /></div> : null}
      {captures?.length ? (
        <ul className="mb-3 divide-y divide-border text-sm">
          {captures.map((c) => (
            <li key={c.id} className="flex items-center justify-between gap-2 py-2">
              <span><Badge tone="info">{c.kind}</Badge> <span className="text-muted">
                {c.characters.toLocaleString()} characters · {new Date(c.captured_at).toLocaleString()}
              </span></span>
              <button type="button" className={buttonClass("ghost")} disabled={busy} onClick={() => remove(c.id)}>Remove</button>
            </li>
          ))}
        </ul>
      ) : null}
      {!open ? (
        <button type="button" className={buttonClass()} onClick={() => setOpen(true)}>Add LinkedIn capture</button>
      ) : (
        <div className="space-y-3">
          <Field label="What are you pasting?" htmlFor="li-kind" hint={KINDS[kind].hint}>
            <select id="li-kind" className={inputClass} value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              {(Object.keys(KINDS) as Kind[]).map((k) => <option key={k} value={k}>{KINDS[k].label}</option>)}
            </select>
          </Field>
          {pageUrl ? (
            <p className="text-sm">
              1. Open <a href={pageUrl} target="_blank" rel="noopener noreferrer" className="text-accent underline">{pageUrl}</a>{" "}
              2. Press Ctrl+A, then Ctrl+C. 3. Paste below.
            </p>
          ) : (
            <Notice tone="warning" title="No LinkedIn URL on file">Add their profile URL (Edit) so the right page opens and indexed posts are found.</Notice>
          )}
          <textarea aria-label="Pasted LinkedIn text" rows={8} className={inputClass} value={text}
                    placeholder="Paste the page text here" onChange={(e) => setText(e.target.value)} />
          <div className="flex gap-2">
            <button type="button" className={buttonClass("primary")} disabled={busy || text.trim().length < 40} onClick={save}>
              {busy ? "Saving…" : "Save and refresh research"}
            </button>
            <button type="button" className={buttonClass("ghost")} onClick={() => setOpen(false)}>Cancel</button>
          </div>
          <p className="text-xs text-muted">
            Sensitive personal topics (health, religion, politics, family) are filtered out, and the writer never says it
            saw their likes or reposts.
          </p>
        </div>
      )}
    </Card>
  );
}
