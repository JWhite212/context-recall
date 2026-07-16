import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  draftFollowupEmail,
  getInsightDefinitions,
  getMeetingAutomations,
  getMeetingInsights,
  getMeetingTrackerHits,
  getTalkStats,
} from "../../lib/api";
import { useToast } from "../common/Toast";
import type {
  EmailDraft,
  InsightDefinition,
  MeetingInsightResult,
} from "../../lib/types";

/** Turns a `field.key` like "go_live_date" into "go live date". */
function humaniseKey(key: string): string {
  return key.replace(/_/g, " ");
}

/** Renders one structured field's value: arrays join, empties become "—". */
function formatFieldValue(value: unknown): string {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) {
    return value.length === 0 ? "—" : value.join("; ");
  }
  return String(value);
}

/** Pills naming the automation rules that fired on a meeting. */
export function AutomationBadges({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {names.map((name, i) => (
        <span
          key={`${name}-${i}`}
          className="text-xs px-2 py-0.5 rounded-full bg-purple-400/20 text-purple-400"
          title="Automation rule that fired for this meeting"
        >
          {name}
        </span>
      ))}
    </div>
  );
}

/** Custom insight results grouped by definition, rendered as labelled lists
 * (or, for structured-mode results, labelled key→value cards). */
export function InsightResults({
  results,
  definitions,
}: {
  results: MeetingInsightResult[];
  definitions?: InsightDefinition[];
}) {
  if (results.length === 0) return null;
  const groups = new Map<string, MeetingInsightResult[]>();
  for (const r of results) {
    const items = groups.get(r.definition_name);
    if (items) items.push(r);
    else groups.set(r.definition_name, [r]);
  }
  const definitionsById = new Map<string, InsightDefinition>();
  for (const def of definitions ?? []) definitionsById.set(def.id, def);
  const labelFor = (definitionId: string, key: string): string => {
    const field = definitionsById
      .get(definitionId)
      ?.fields?.find((f) => f.key === key);
    return field?.label ?? humaniseKey(key);
  };
  return (
    <div className="flex flex-col gap-2">
      {[...groups.entries()].map(([name, items]) => {
        const listItems = items.filter((item) => item.fields == null);
        const fieldItems = items.filter((item) => item.fields != null);
        return (
          <div key={name}>
            <p className="text-[10px] uppercase tracking-wide text-text-muted mb-1">
              {name}
            </p>
            {listItems.length > 0 && (
              <ul className="flex flex-col gap-0.5">
                {listItems.map((item, i) => (
                  <li
                    key={`${name}-list-${i}`}
                    className="text-xs text-text-secondary flex gap-1.5"
                  >
                    <span className="text-text-muted/60">•</span>
                    <span>
                      {item.content}
                      {item.speaker && (
                        <span className="text-text-muted/60">
                          {" "}
                          — {item.speaker}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {fieldItems.length > 0 && (
              <div className="flex flex-col gap-1.5">
                {fieldItems.map((item, i) => (
                  <div
                    key={`${name}-card-${i}`}
                    className="rounded-md border border-border bg-surface p-2 flex flex-col gap-1"
                  >
                    {Object.entries(item.fields as Record<string, unknown>).map(
                      ([key, value]) => (
                        <div
                          key={key}
                          className="flex justify-between gap-3 text-xs"
                        >
                          <span className="text-text-muted">
                            {labelFor(item.definition_id, key)}
                          </span>
                          <span className="text-text-secondary text-right">
                            {formatFieldValue(value)}
                          </span>
                        </div>
                      ),
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

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

  const { data: insightResults = [] } = useQuery({
    queryKey: ["meeting-insights", meetingId],
    queryFn: () => getMeetingInsights(meetingId),
  });

  const { data: insightDefinitions = [] } = useQuery({
    queryKey: ["insight-definitions"],
    queryFn: getInsightDefinitions,
  });

  const { data: firedAutomations = [] } = useQuery({
    queryKey: ["meeting-automations", meetingId],
    queryFn: () => getMeetingAutomations(meetingId),
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

  if (
    speakers.length === 0 &&
    hitsByTracker.size === 0 &&
    insightResults.length === 0 &&
    firedAutomations.length === 0 &&
    !hasSummary
  ) {
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

      {/* Custom insights */}
      <InsightResults
        results={insightResults}
        definitions={insightDefinitions}
      />

      {/* Fired automation rules */}
      <AutomationBadges names={firedAutomations.map((a) => a.name)} />

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
