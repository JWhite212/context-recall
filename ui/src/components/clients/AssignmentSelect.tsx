import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClients, getProjects, setMeetingAssignment } from "../../lib/api";
import { useToast } from "../common/Toast";
import type { Meeting } from "../../lib/types";

/**
 * Compact client/project assignment control for a meeting. Shows how
 * the current assignment was made (auto vs manual); changing it makes
 * the assignment manual, which the automatic passes never overwrite.
 */
export function AssignmentSelect({ meeting }: { meeting: Meeting }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: clients = [] } = useQuery({
    queryKey: ["clients"],
    queryFn: () => getClients(),
  });
  const { data: projects = [] } = useQuery({
    queryKey: ["projects"],
    queryFn: () => getProjects(),
  });

  const mutate = useMutation({
    mutationFn: ({
      clientId,
      projectId,
    }: {
      clientId: string | null;
      projectId: string | null;
    }) => setMeetingAssignment(meeting.id, clientId, projectId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["meeting", meeting.id] });
      void queryClient.invalidateQueries({ queryKey: ["meetings"] });
    },
    onError: () => toast.error("Failed to update assignment"),
  });

  if (clients.length === 0 && projects.length === 0) {
    return null; // Nothing to assign to yet — the Clients screen seeds this.
  }

  const clientId = meeting.client_id ?? null;
  const projectId = meeting.project_id ?? null;
  const clientProjects = projects.filter(
    (p) => !clientId || p.client_id === clientId || p.client_id === null,
  );
  const source = meeting.assignment_source ?? "";

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        value={clientId ?? ""}
        onChange={(e) => {
          const next = e.target.value || null;
          // Changing client invalidates a project belonging to another client.
          const keepProject =
            projectId &&
            projects.some((p) => p.id === projectId && p.client_id === next);
          mutate.mutate({
            clientId: next,
            projectId: keepProject ? projectId : null,
          });
        }}
        className="px-2 py-1 text-xs rounded bg-surface border border-border text-text-primary cursor-pointer"
        aria-label="Assign client"
      >
        <option value="">No client</option>
        {clients.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      <select
        value={projectId ?? ""}
        onChange={(e) =>
          mutate.mutate({ clientId, projectId: e.target.value || null })
        }
        className="px-2 py-1 text-xs rounded bg-surface border border-border text-text-primary cursor-pointer"
        aria-label="Assign project"
      >
        <option value="">No project</option>
        {clientProjects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      {source === "auto" && (
        <span
          className="text-xs px-2 py-0.5 rounded-full bg-amber-400/20 text-amber-400"
          title={`Assigned automatically (${Math.round((meeting.assignment_confidence ?? 0) * 100)}% confidence). Change it to lock it in manually.`}
        >
          auto
        </span>
      )}
      {source === "manual" && (
        <span className="text-xs px-2 py-0.5 rounded-full bg-green-400/20 text-green-400">
          manual
        </span>
      )}
    </div>
  );
}
