import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createClient,
  createProject,
  deleteClient,
  deleteProject,
  getClients,
  getProjects,
  updateClient,
  updateProject,
} from "../../lib/api";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import { useToast } from "../common/Toast";
import type { Client, Project } from "../../lib/types";

function EntityForm({
  title,
  initial,
  showDomains,
  onSave,
  onCancel,
  saving,
}: {
  title: string;
  initial: {
    name: string;
    description: string;
    aliases: string;
    domains: string;
  };
  showDomains: boolean;
  onSave: (form: {
    name: string;
    description: string;
    aliases: string;
    domains: string;
  }) => void;
  onCancel: () => void;
  saving: boolean;
}) {
  const [form, setForm] = useState(initial);
  return (
    <div className="flex flex-col gap-2 py-2 px-3 rounded-lg bg-sidebar-hover">
      <p className="text-xs font-medium text-text-primary">{title}</p>
      <input
        value={form.name}
        onChange={(e) => setForm({ ...form, name: e.target.value })}
        placeholder="Name"
        className="px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary"
      />
      <textarea
        value={form.description}
        onChange={(e) => setForm({ ...form, description: e.target.value })}
        placeholder="Description — this text is given to the AI as context when summarising and auto-tagging meetings, so include what matters: who they are, terminology, key people, current focus…"
        rows={3}
        className="px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary resize-none"
      />
      <div className="flex gap-2">
        <input
          value={form.aliases}
          onChange={(e) => setForm({ ...form, aliases: e.target.value })}
          placeholder="Aliases (comma-separated)"
          className="flex-1 px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary"
        />
        {showDomains && (
          <input
            value={form.domains}
            onChange={(e) => setForm({ ...form, domains: e.target.value })}
            placeholder="Email domains, e.g. acme.com"
            className="flex-1 px-2 py-1 text-sm rounded bg-surface border border-border text-text-primary"
          />
        )}
      </div>
      <div className="flex gap-2 justify-end">
        <button
          onClick={onCancel}
          className="text-xs text-text-muted hover:underline cursor-pointer"
        >
          Cancel
        </button>
        <button
          onClick={() => onSave(form)}
          disabled={!form.name.trim() || saving}
          className="text-xs px-3 py-1 rounded bg-accent text-white disabled:opacity-50 cursor-pointer"
        >
          Save
        </button>
      </div>
    </div>
  );
}

const splitList = (value: string) =>
  value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

function ProjectRow({ project }: { project: Project }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);

  const save = useMutation({
    mutationFn: (form: {
      name: string;
      description: string;
      aliases: string;
    }) =>
      updateProject(project.id, {
        name: form.name.trim(),
        description: form.description,
        aliases: splitList(form.aliases),
      }),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: () => toast.error("Failed to update project"),
  });

  const remove = useMutation({
    mutationFn: () => deleteProject(project.id),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["projects"] }),
    onError: () => toast.error("Failed to delete project"),
  });

  if (editing) {
    return (
      <EntityForm
        title="Edit project"
        initial={{
          name: project.name,
          description: project.description,
          aliases: project.aliases.join(", "),
          domains: "",
        }}
        showDomains={false}
        onSave={(form) => save.mutate(form)}
        onCancel={() => setEditing(false)}
        saving={save.isPending}
      />
    );
  }

  return (
    <div className="flex items-center justify-between py-1 pl-6 pr-3 rounded-lg hover:bg-sidebar-hover">
      <div className="min-w-0">
        <p className="text-sm text-text-primary truncate">{project.name}</p>
        {project.description && (
          <p className="text-xs text-text-muted truncate">
            {project.description}
          </p>
        )}
      </div>
      <div className="flex items-center gap-3 ml-3 whitespace-nowrap">
        <button
          onClick={() => setEditing(true)}
          className="text-xs text-text-muted hover:text-text-primary hover:underline cursor-pointer"
        >
          Edit
        </button>
        <button
          onClick={() => {
            if (window.confirm(`Delete project "${project.name}"?`))
              remove.mutate();
          }}
          className="text-xs text-rose-400 hover:underline cursor-pointer"
        >
          Delete
        </button>
      </div>
    </div>
  );
}

function ClientCard({
  client,
  projects,
}: {
  client: Client;
  projects: Project[];
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [addingProject, setAddingProject] = useState(false);

  const save = useMutation({
    mutationFn: (form: {
      name: string;
      description: string;
      aliases: string;
      domains: string;
    }) =>
      updateClient(client.id, {
        name: form.name.trim(),
        description: form.description,
        aliases: splitList(form.aliases),
        email_domains: splitList(form.domains),
      }),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["clients"] });
    },
    onError: () => toast.error("Failed to update client"),
  });

  const remove = useMutation({
    mutationFn: () => deleteClient(client.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["clients"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: () => toast.error("Failed to delete client"),
  });

  const addProject = useMutation({
    mutationFn: (form: {
      name: string;
      description: string;
      aliases: string;
    }) =>
      createProject({
        name: form.name.trim(),
        client_id: client.id,
        description: form.description,
        aliases: splitList(form.aliases),
      }),
    onSuccess: () => {
      setAddingProject(false);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: () => toast.error("Failed to create project"),
  });

  return (
    <div className="rounded-xl bg-surface-raised border border-border p-3 flex flex-col gap-1">
      {editing ? (
        <EntityForm
          title="Edit client"
          initial={{
            name: client.name,
            description: client.description,
            aliases: client.aliases.join(", "),
            domains: client.email_domains.join(", "),
          }}
          showDomains
          onSave={(form) => save.mutate(form)}
          onCancel={() => setEditing(false)}
          saving={save.isPending}
        />
      ) : (
        <div className="flex items-start justify-between py-1 px-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold text-text-primary truncate">
                {client.name}
              </p>
              {client.email_domains.length > 0 && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-blue-400/20 text-blue-400">
                  @{client.email_domains[0]}
                  {client.email_domains.length > 1
                    ? ` +${client.email_domains.length - 1}`
                    : ""}
                </span>
              )}
            </div>
            {client.description && (
              <p className="text-xs text-text-muted line-clamp-2">
                {client.description}
              </p>
            )}
          </div>
          <div className="flex items-center gap-3 ml-3 whitespace-nowrap">
            <button
              onClick={() => setAddingProject(true)}
              className="text-xs text-accent hover:underline cursor-pointer"
            >
              + Project
            </button>
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
                    `Delete ${client.name}? Meetings keep their history but lose this assignment.`,
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
      )}
      {addingProject && (
        <EntityForm
          title={`New project for ${client.name}`}
          initial={{ name: "", description: "", aliases: "", domains: "" }}
          showDomains={false}
          onSave={(form) => addProject.mutate(form)}
          onCancel={() => setAddingProject(false)}
          saving={addProject.isPending}
        />
      )}
      {projects.map((p) => (
        <ProjectRow key={p.id} project={p} />
      ))}
    </div>
  );
}

export function ClientsView() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);

  const clientsQuery = useQuery({
    queryKey: ["clients"],
    queryFn: () => getClients(),
  });
  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: () => getProjects(),
  });

  const addClient = useMutation({
    mutationFn: (form: {
      name: string;
      description: string;
      aliases: string;
      domains: string;
    }) =>
      createClient({
        name: form.name.trim(),
        description: form.description,
        aliases: splitList(form.aliases),
        email_domains: splitList(form.domains),
      }),
    onSuccess: () => {
      setAdding(false);
      void queryClient.invalidateQueries({ queryKey: ["clients"] });
    },
    onError: () => toast.error("Failed to create client"),
  });

  const clients = clientsQuery.data ?? [];
  const projects = projectsQuery.data ?? [];
  const unlinkedProjects = projects.filter(
    (p) => !p.client_id || !clients.some((c) => c.id === p.client_id),
  );
  const isLoading = clientsQuery.isLoading || projectsQuery.isLoading;
  const isError = clientsQuery.isError || projectsQuery.isError;

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h1 className="text-lg font-semibold text-text-primary">
            Clients & Projects
          </h1>
          {!isLoading && !isError && (
            <span className="text-xs text-text-muted">({clients.length})</span>
          )}
        </div>
        <button
          onClick={() => setAdding(true)}
          className="text-sm px-4 py-2 rounded-lg bg-accent text-white cursor-pointer"
        >
          Add client
        </button>
      </div>

      <p className="text-xs text-text-muted">
        Meetings are auto-assigned from attendee email domains, calendar titles,
        recurring-series history, and meeting content. Descriptions you write
        here are fed to the AI while summarising and tagging — richer
        descriptions mean better summaries and more accurate auto-assignment.
      </p>

      {adding && (
        <EntityForm
          title="New client"
          initial={{ name: "", description: "", aliases: "", domains: "" }}
          showDomains
          onSave={(form) => addClient.mutate(form)}
          onCancel={() => setAdding(false)}
          saving={addClient.isPending}
        />
      )}

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
          message="Failed to load clients."
          onRetry={() => {
            void clientsQuery.refetch();
            void projectsQuery.refetch();
          }}
        />
      ) : clients.length === 0 && unlinkedProjects.length === 0 ? (
        <EmptyState
          title="No clients yet"
          description="Add the clients and projects you meet about. Context Recall will start assigning meetings to them automatically."
        />
      ) : (
        <>
          {clients.map((c) => (
            <ClientCard
              key={c.id}
              client={c}
              projects={projects.filter((p) => p.client_id === c.id)}
            />
          ))}
          {unlinkedProjects.length > 0 && (
            <div className="rounded-xl bg-surface-raised border border-border p-3 flex flex-col gap-1">
              <p className="text-sm font-semibold text-text-primary py-1 px-3">
                Projects without a client
              </p>
              {unlinkedProjects.map((p) => (
                <ProjectRow key={p.id} project={p} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
