import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { setSpeakerName } from "../../lib/api";
import { useToast } from "../common/Toast";
import type { TranscriptSegment } from "../../lib/types";

/** Alter-style speaker-correction panel: list detected speakers, play each
 *  speaker's segments, and rename one (propagates across the transcript and
 *  persists via the speaker-mappings table). */
export function SpeakerPanel({
  meetingId,
  segments,
  onSeek,
}: {
  meetingId: string;
  segments: TranscriptSegment[];
  onSeek: (seconds: number) => void;
}) {
  const speakers = useMemo(() => {
    const map = new Map<string, { count: number; first: number }>();
    for (const s of segments) {
      if (!s.speaker) continue;
      const e = map.get(s.speaker);
      if (e) e.count += 1;
      else map.set(s.speaker, { count: 1, first: s.start });
    }
    return [...map.entries()].map(([speaker, v]) => ({ speaker, ...v }));
  }, [segments]);

  if (speakers.length === 0) return null;

  return (
    <div className="rounded-xl bg-surface-raised border border-border p-4">
      <h2 className="text-sm font-medium text-text-primary">Speakers</h2>
      <p className="text-xs text-text-muted mt-0.5">
        Play a speaker's parts, then rename them — the change applies across the
        transcript and is kept when you reprocess.
      </p>
      <ul className="mt-3 flex flex-col gap-2">
        {speakers.map((s) => (
          <SpeakerRow
            key={s.speaker}
            meetingId={meetingId}
            speaker={s.speaker}
            count={s.count}
            firstStart={s.first}
            onSeek={onSeek}
          />
        ))}
      </ul>
    </div>
  );
}

function SpeakerRow({
  meetingId,
  speaker,
  count,
  firstStart,
  onSeek,
}: {
  meetingId: string;
  speaker: string;
  count: number;
  firstStart: number;
  onSeek: (seconds: number) => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(speaker);

  const rename = useMutation({
    mutationFn: (next: string) => setSpeakerName(meetingId, speaker, next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      setEditing(false);
    },
    onError: () => {
      toast.error("Failed to rename speaker.");
      setEditing(false);
    },
  });

  function commit() {
    const next = value.trim();
    if (!next || next === speaker) {
      setValue(speaker);
      setEditing(false);
      return;
    }
    rename.mutate(next);
  }

  return (
    <li className="flex items-center gap-2 text-sm">
      <button
        type="button"
        aria-label={`Play ${speaker} segments`}
        onClick={() => onSeek(firstStart)}
        className="px-2 py-1 text-xs rounded-lg bg-accent/10 text-accent hover:bg-accent/20"
      >
        ▶ Play
      </button>
      {editing ? (
        <input
          autoFocus
          value={value}
          disabled={rename.isPending}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setValue(speaker);
              setEditing(false);
            }
          }}
          className="bg-surface border border-border rounded px-1 text-text-primary"
        />
      ) : (
        <span className="font-medium text-text-primary">{speaker}</span>
      )}
      <span className="text-xs text-text-muted">
        {count} segment{count === 1 ? "" : "s"}
      </span>
      {!editing && (
        <button
          type="button"
          aria-label={`Rename ${speaker}`}
          onClick={() => {
            setValue(speaker);
            setEditing(true);
          }}
          className="ml-auto px-2 py-1 text-xs rounded-lg text-text-secondary hover:text-text-primary"
        >
          Rename
        </button>
      )}
    </li>
  );
}
