import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createPerson,
  deletePerson,
  deleteVoiceSample,
  getPeople,
  getVoiceSamples,
  updatePerson,
} from "../../lib/api";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import { useToast } from "../common/Toast";
import type { Person } from "../../lib/types";

function VoiceSamples({ person }: { person: Person }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data: samples = [], isLoading } = useQuery({
    queryKey: ["voice-samples", person.id],
    queryFn: () => getVoiceSamples(person.id),
  });

  const removeSample = useMutation({
    mutationFn: (sampleId: number) => deleteVoiceSample(person.id, sampleId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["voice-samples", person.id],
      });
      void queryClient.invalidateQueries({ queryKey: ["people"] });
    },
    onError: () => toast.error("Failed to delete voice sample"),
  });

  if (isLoading) {
    return (
      <p className="text-xs text-text-muted px-3 pb-2">Loading samples…</p>
    );
  }
  if (samples.length === 0) {
    return (
      <p className="text-xs text-text-muted px-3 pb-2">
        No voice samples yet. Assign this person to a speaker in a meeting
        transcript to enrol their voice.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-1 px-3 pb-2">
      {samples.map((s) => (
        <div
          key={s.id}
          className="flex items-center justify-between text-xs text-text-muted"
        >
          <span>
            {s.segment_count} segment{s.segment_count === 1 ? "" : "s"},{" "}
            {Math.round(s.duration_seconds)}s of speech
            {s.speaker_label ? ` (as "${s.speaker_label}")` : ""}
          </span>
          <button
            onClick={() => removeSample.mutate(s.id)}
            className="text-rose-400 hover:underline cursor-pointer"
            title="Delete this voice sample"
          >
            Remove
          </button>
        </div>
      ))}
    </div>
  );
}

function PersonRow({ person }: { person: Person }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({
    name: person.name,
    email: person.email,
    aliases: person.aliases.join(", "),
    notes: person.notes,
  });

  const save = useMutation({
    mutationFn: () =>
      updatePerson(person.id, {
        name: form.name.trim(),
        email: form.email.trim(),
        aliases: form.aliases
          .split(",")
          .map((a) => a.trim())
          .filter(Boolean),
        notes: form.notes,
      }),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["people"] });
    },
    onError: () => toast.error("Failed to update person"),
  });

  const remove = useMutation({
    mutationFn: () => deletePerson(person.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["people"] });
    },
    onError: () => toast.error("Failed to delete person"),
  });

  if (editing) {
    return (
      <div className="flex flex-col gap-2 py-2 px-3 rounded-lg bg-sidebar-hover">
        <div className="flex gap-2">
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Name"
            className="flex-1 px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary"
          />
          <input
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            placeholder="Email"
            className="flex-1 px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary"
          />
        </div>
        <input
          value={form.aliases}
          onChange={(e) => setForm({ ...form, aliases: e.target.value })}
          placeholder="Aliases (comma-separated)"
          className="px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary"
        />
        <textarea
          value={form.notes}
          onChange={(e) => setForm({ ...form, notes: e.target.value })}
          placeholder="Notes"
          rows={2}
          className="px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary resize-none"
        />
        <div className="flex gap-2 justify-end">
          <button
            onClick={() => setEditing(false)}
            className="text-xs text-text-muted hover:underline cursor-pointer"
          >
            Cancel
          </button>
          <button
            onClick={() => save.mutate()}
            disabled={!form.name.trim() || save.isPending}
            className="text-xs px-3 py-1 rounded bg-accent text-white disabled:opacity-50 cursor-pointer"
          >
            Save
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg hover:bg-sidebar-hover transition-colors">
      <div className="flex items-center justify-between py-2 px-3">
        <button
          onClick={() => setExpanded(!expanded)}
          className="min-w-0 flex items-center gap-2 text-left cursor-pointer"
        >
          <p className="text-sm font-medium text-text-primary truncate">
            {person.name}
          </p>
          {person.is_me && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-blue-400/20 text-blue-400">
              me
            </span>
          )}
          {person.sample_count > 0 ? (
            <span
              className="text-xs px-2 py-0.5 rounded-full bg-green-400/20 text-green-400"
              title="This person's voice is enrolled and will be recognised automatically"
            >
              voice ✓ ({person.sample_count})
            </span>
          ) : (
            <span className="text-xs px-2 py-0.5 rounded-full bg-amber-400/20 text-amber-400">
              no voice profile
            </span>
          )}
          {person.email && (
            <span className="text-xs text-text-muted truncate">
              {person.email}
            </span>
          )}
        </button>
        <div className="flex items-center gap-3 ml-3 whitespace-nowrap">
          <button
            onClick={() => setEditing(true)}
            className="text-xs text-text-muted hover:text-text-primary hover:underline cursor-pointer"
          >
            Edit
          </button>
          <button
            onClick={() => {
              if (
                window.confirm(
                  `Delete ${person.name}? Their voice profile will be removed; past transcripts keep their name.`,
                )
              ) {
                remove.mutate();
              }
            }}
            className="text-xs text-rose-400 hover:underline cursor-pointer"
          >
            Delete
          </button>
        </div>
      </div>
      {expanded && (
        <>
          {person.notes && (
            <p className="text-xs text-text-muted px-3 pb-2 whitespace-pre-wrap">
              {person.notes}
            </p>
          )}
          <VoiceSamples person={person} />
        </>
      )}
    </div>
  );
}

export function PeopleView() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [newName, setNewName] = useState("");

  const {
    data: people = [],
    isLoading,
    isError,
    refetch,
  } = useQuery({
    queryKey: ["people"],
    queryFn: getPeople,
  });

  const add = useMutation({
    mutationFn: () => createPerson({ name: newName.trim() }),
    onSuccess: () => {
      setNewName("");
      void queryClient.invalidateQueries({ queryKey: ["people"] });
    },
    onError: () => toast.error("Failed to add person"),
  });

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-semibold text-text-primary">People</h1>
        {!isLoading && !isError && (
          <span className="text-xs text-text-muted">({people.length})</span>
        )}
      </div>

      <p className="text-xs text-text-muted">
        Your regular meeting attendees. Assign a person to a transcript speaker
        once and Context Recall learns their voice — future meetings label them
        automatically, helped by calendar attendee lists and how people
        introduce themselves.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (newName.trim()) add.mutate();
        }}
        className="flex gap-2"
      >
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="Add a person by name…"
          className="flex-1 px-3 py-2 text-sm rounded-lg bg-surface-raised border border-border text-text-primary"
        />
        <button
          type="submit"
          disabled={!newName.trim() || add.isPending}
          className="text-sm px-4 py-2 rounded-lg bg-accent text-white disabled:opacity-50 cursor-pointer"
        >
          Add
        </button>
      </form>

      {isLoading ? (
        <div className="rounded-xl bg-surface-raised border border-border p-6">
          <div className="flex flex-col gap-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <SkeletonCard key={i} />
            ))}
          </div>
        </div>
      ) : isError ? (
        <ErrorState
          message="Failed to load people."
          onRetry={() => refetch()}
        />
      ) : people.length === 0 ? (
        <EmptyState
          title="No people yet"
          description="Add the people you meet with regularly, then assign them to speakers in a meeting transcript to enrol their voices."
        />
      ) : (
        <div className="rounded-xl bg-surface-raised border border-border p-3">
          <div className="flex flex-col gap-1">
            {people.map((p) => (
              // updated_at in the key remounts the row (and its edit form
              // state) whenever the underlying person record changes.
              <PersonRow key={`${p.id}-${p.updated_at}`} person={p} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
