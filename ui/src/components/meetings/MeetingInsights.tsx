import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  draftFollowupEmail,
  getMeetingTrackerHits,
  getTalkStats,
} from "../../lib/api";
import { useToast } from "../common/Toast";
import type { EmailDraft } from "../../lib/types";

const BAR_COLORS = [
  "bg-blue-400",
  "bg-emerald-400",
  "bg-amber-400",
  "bg-purple-400",
  "bg-rose-400",
  "bg-cyan-400",
];

/** Talk-time bar + tracker mentions + follow-up email draft for a meeting. */
export function MeetingInsights({
  meetingId,
  hasSummary,
}: {
  meetingId: string;
  hasSummary: boolean;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<EmailDraft | null>(null);
  const [copied, setCopied] = useState(false);

  const { data: talkStats } = useQuery({
    queryKey: ["talk-stats", meetingId],
    queryFn: () => getTalkStats(meetingId),
  });

  const { data: trackerHits = [] } = useQuery({
    queryKey: ["meeting-tracker-hits", meetingId],
    queryFn: () => getMeetingTrackerHits(meetingId),
  });

  const email = useMutation({
    mutationFn: () => draftFollowupEmail(meetingId),
    onSuccess: setDraft,
    onError: () =>
      toast.error(
        "Could not draft the email — check the summarisation backend.",
      ),
  });

  const speakers = (talkStats?.speakers ?? []).filter(
    (s) => s.speaker !== "Unlabelled",
  );
  const hitsByTracker = new Map<string, number>();
  for (const hit of trackerHits) {
    const name = hit.tracker_name || "Tracker";
    hitsByTracker.set(name, (hitsByTracker.get(name) ?? 0) + 1);
  }

  if (speakers.length === 0 && hitsByTracker.size === 0 && !hasSummary) {
    return null;
  }

  return (
    <div className="mt-3 rounded-lg bg-surface-raised border border-border p-3 flex flex-col gap-2">
      {/* Talk time */}
      {speakers.length > 0 && (
        <div>
          <p className="text-[10px] uppercase tracking-wide text-text-muted mb-1.5">
            Talk time
          </p>
          <div
            className="flex h-2 rounded-full overflow-hidden bg-surface"
            role="img"
            aria-label="Talk time distribution"
          >
            {speakers.map((s, i) => (
              <div
                key={s.speaker}
                className={BAR_COLORS[i % BAR_COLORS.length]}
                style={{ width: `${s.percent}%` }}
                title={`${s.speaker}: ${s.percent}%`}
              />
            ))}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1.5">
            {speakers.map((s, i) => (
              <span key={s.speaker} className="text-xs text-text-muted">
                <span
                  className={`inline-block w-2 h-2 rounded-full mr-1 ${BAR_COLORS[i % BAR_COLORS.length]}`}
                />
                {s.speaker} {s.percent}%
                <span className="text-text-muted/60">
                  {" "}
                  · {s.turns} turn{s.turns === 1 ? "" : "s"}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Tracker mentions */}
      {hitsByTracker.size > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {[...hitsByTracker.entries()].map(([name, count]) => (
            <span
              key={name}
              className="text-xs px-2 py-0.5 rounded-full bg-amber-400/20 text-amber-400"
              title="Keyword tracker mentions in this meeting"
            >
              {name}: {count}
            </span>
          ))}
        </div>
      )}

      {/* Follow-up email */}
      {hasSummary && (
        <div>
          <button
            onClick={() => email.mutate()}
            disabled={email.isPending}
            className="text-xs px-3 py-1.5 rounded-lg bg-surface border border-border text-text-primary hover:bg-sidebar-hover disabled:opacity-50 cursor-pointer"
          >
            {email.isPending ? "Drafting…" : "Draft follow-up email"}
          </button>
          {draft && (
            <div className="mt-2 rounded-lg bg-surface border border-border p-3">
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs font-medium text-text-primary">
                  {draft.subject}
                </p>
                <button
                  onClick={() => {
                    navigator.clipboard
                      .writeText(`Subject: ${draft.subject}\n\n${draft.body}`)
                      .then(() => {
                        setCopied(true);
                        setTimeout(() => setCopied(false), 1500);
                      })
                      .catch(() => toast.error("Copy failed"));
                  }}
                  className="text-xs text-accent hover:underline cursor-pointer"
                >
                  {copied ? "Copied ✓" : "Copy"}
                </button>
              </div>
              <pre className="text-xs text-text-muted whitespace-pre-wrap font-sans">
                {draft.body}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
