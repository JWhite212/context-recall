import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { assignPersonToSpeaker, createPerson, getPeople } from "../../lib/api";
import { useToast } from "../common/Toast";

/**
 * Small popover on a transcript speaker label: assign the speaker to a
 * person from the directory (enrolling their voice from this meeting),
 * or create a new person on the spot.
 */
export function AssignSpeakerMenu({
  speaker,
  meetingId,
  onAssigned,
}: {
  speaker: string;
  meetingId: string;
  onAssigned: () => void;
}) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: people = [] } = useQuery({
    queryKey: ["people"],
    queryFn: getPeople,
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const assign = useMutation({
    mutationFn: async (personId: string) =>
      assignPersonToSpeaker(meetingId, speaker, personId, true),
    onSuccess: (result) => {
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["people"] });
      if (result.enrolled) {
        toast.success(
          `${result.display_name} assigned — voice enrolled (${result.sample_count} sample${result.sample_count === 1 ? "" : "s"})`,
        );
      } else {
        toast.info(
          `${result.display_name} assigned${result.reason ? ` — ${result.reason}` : ""}`,
        );
      }
      onAssigned();
    },
    onError: () => toast.error("Failed to assign person"),
  });

  const createAndAssign = useMutation({
    mutationFn: async () => {
      const person = await createPerson({ name: speaker });
      return assignPersonToSpeaker(meetingId, speaker, person.id, true);
    },
    onSuccess: (result) => {
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["people"] });
      toast.success(`Added ${result.display_name} to People`);
      onAssigned();
    },
    onError: () => toast.error("Failed to create person"),
  });

  return (
    <span className="relative inline-block" ref={menuRef}>
      <button
        onClick={() => setOpen(!open)}
        className="text-text-muted hover:text-text-primary cursor-pointer align-middle"
        title="Assign this speaker to a person"
        aria-label={`Assign speaker ${speaker} to a person`}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
      </button>
      {open && (
        <div className="absolute z-20 left-0 top-5 min-w-44 rounded-lg bg-surface-raised border border-border shadow-lg py-1">
          <p className="px-3 py-1 text-[10px] uppercase tracking-wide text-text-muted">
            Assign to person
          </p>
          {people.map((p) => (
            <button
              key={p.id}
              onClick={() => assign.mutate(p.id)}
              disabled={assign.isPending}
              className="w-full text-left px-3 py-1.5 text-xs text-text-primary hover:bg-sidebar-hover cursor-pointer disabled:opacity-50"
            >
              {p.name}
              {p.sample_count > 0 && (
                <span className="text-green-400 ml-1" title="Voice enrolled">
                  ●
                </span>
              )}
            </button>
          ))}
          {people.length === 0 && (
            <p className="px-3 py-1.5 text-xs text-text-muted">No people yet</p>
          )}
          <button
            onClick={() => createAndAssign.mutate()}
            disabled={createAndAssign.isPending}
            className="w-full text-left px-3 py-1.5 text-xs text-accent hover:bg-sidebar-hover cursor-pointer border-t border-border mt-1 disabled:opacity-50"
          >
            + New person “{speaker}”
          </button>
        </div>
      )}
    </span>
  );
}
