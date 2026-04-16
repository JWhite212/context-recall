import { useState, useEffect, useMemo, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useBlocker } from "react-router-dom";
import { invoke } from "@tauri-apps/api/core";
import {
  getConfig,
  updateConfig,
  getModels,
  downloadModel,
  getTemplates,
  saveTemplate,
  deleteTemplate,
} from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useAppStore } from "../../stores/appStore";
import { useTheme } from "../../hooks/useTheme";
import { useToast } from "../common/Toast";
import { Tooltip } from "../common/Tooltip";
import type { Theme } from "../../hooks/useTheme";
import type { AppConfig, WhisperModel, SummaryTemplate } from "../../lib/types";

/* ------------------------------------------------------------------ */
/*  Reusable form primitives                                          */
/* ------------------------------------------------------------------ */

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        checked ? "bg-accent" : "bg-border"
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-[2px]"
        }`}
      />
    </button>
  );
}

function Field({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  const fieldId = label.toLowerCase().replace(/\s+/g, "-");
  return (
    <div className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0 pt-1">
        <label htmlFor={fieldId} className="text-sm text-text-primary">
          {label}
        </label>
        {help && <p className="text-xs text-text-muted mt-0.5">{help}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="rounded-xl bg-surface-raised border border-border p-5">
      <legend className="sr-only">{title}</legend>
      <h2 className="text-sm font-medium text-text-primary">{title}</h2>
      {description && (
        <p className="text-xs text-text-muted mt-1">{description}</p>
      )}
      <div className="divide-y divide-border mt-3">{children}</div>
    </fieldset>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between py-2">
      <span className="text-xs text-text-muted">{label}</span>
      <span className="text-sm text-text-primary font-mono">{value}</span>
    </div>
  );
}

function ModelSection({
  models,
  onDownload,
  downloading,
}: {
  models: WhisperModel[];
  onDownload: (name: string) => void;
  downloading: boolean;
}) {
  const modelProgress = useAppStore((s) => s.modelProgress);

  return (
    <Section
      title="Whisper Models"
      description="Download and manage transcription models"
    >
      {models.map((model) => {
        const liveProgress = modelProgress[model.name];
        const isDownloading =
          model.status === "downloading" ||
          (liveProgress && liveProgress.percent < 100 && !liveProgress.error);
        const percent = liveProgress?.percent ?? model.percent;

        return (
          <div key={model.name} className="flex flex-col gap-2 py-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm text-text-primary">{model.name}</div>
                <div className="text-xs text-text-muted">
                  {model.size_mb} MB
                </div>
              </div>
              <div className="flex items-center gap-2">
                {model.status === "downloaded" && !isDownloading ? (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-status-idle/20 text-status-idle">
                    Downloaded
                  </span>
                ) : isDownloading ? (
                  <span className="text-xs text-blue-400 tabular-nums">
                    {percent}%
                  </span>
                ) : model.status === "error" || liveProgress?.error ? (
                  <div className="flex items-center gap-2">
                    <span
                      className="text-xs text-status-error"
                      title={liveProgress?.error ?? model.error ?? ""}
                    >
                      Failed
                    </span>
                    <button
                      onClick={() => onDownload(model.name)}
                      className="text-xs px-2 py-0.5 rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
                    >
                      Retry
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => onDownload(model.name)}
                    disabled={downloading}
                    className="text-xs px-3 py-1 rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
                  >
                    Download
                  </button>
                )}
              </div>
            </div>
            {isDownloading && (
              <div
                role="progressbar"
                aria-valuenow={percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`Downloading ${model.name}: ${percent}%`}
                className="h-1.5 rounded-full bg-border overflow-hidden"
              >
                <div
                  className="h-full rounded-full bg-accent transition-[width] duration-300"
                  style={{ width: `${percent}%` }}
                />
              </div>
            )}
          </div>
        );
      })}
    </Section>
  );
}

function UpdateChecker() {
  const [checking, setChecking] = useState(false);
  const [updateVersion, setUpdateVersion] = useState<string | null>(null);
  const [installing, setInstalling] = useState(false);
  const [checked, setChecked] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function checkForUpdates() {
    setChecking(true);
    setError(null);
    try {
      const version = await invoke<string | null>("check_for_updates");
      setUpdateVersion(version);
      setChecked(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setChecking(false);
    }
  }

  async function installUpdate() {
    setInstalling(true);
    setError(null);
    try {
      await invoke("install_update");
      // If the app hasn't restarted, prompt the user.
      setError("Update installed. Please restart the application.");
    } catch (e) {
      setError(String(e));
    } finally {
      setInstalling(false);
    }
  }

  return (
    <div className="py-3">
      {updateVersion ? (
        <div className="flex items-center justify-between">
          <div>
            <span className="text-sm text-text-primary">
              Update available: v{updateVersion}
            </span>
          </div>
          <button
            onClick={installUpdate}
            disabled={installing}
            className="text-xs px-3 py-1 rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
          >
            {installing ? "Installing..." : "Install & Restart"}
          </button>
        </div>
      ) : (
        <div className="flex items-center justify-between">
          <span className="text-sm text-text-secondary">
            {checked ? "You're up to date." : "Check for updates"}
          </span>
          <button
            onClick={checkForUpdates}
            disabled={checking}
            className="text-xs px-3 py-1 rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors disabled:opacity-50"
          >
            {checking ? "Checking..." : "Check Now"}
          </button>
        </div>
      )}
      {error && <p className="text-xs text-status-error mt-1">{error}</p>}
    </div>
  );
}

const BUILT_IN_TEMPLATES = [
  "standard",
  "standup",
  "retro",
  "1on1",
  "client-call",
];

function TemplatesSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [expandedTemplate, setExpandedTemplate] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<{
    name: string;
    description: string;
    system_prompt: string;
    sections: string;
  }>({ name: "", description: "", system_prompt: "", sections: "" });

  const { data: templates = [], isLoading } = useQuery({
    queryKey: ["templates"],
    queryFn: getTemplates,
  });

  const saveMutation = useMutation({
    mutationFn: (template: SummaryTemplate) => saveTemplate(template),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      setCreating(false);
      setExpandedTemplate(null);
      setEditForm({
        name: "",
        description: "",
        system_prompt: "",
        sections: "",
      });
      toast.success("Template saved.");
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : "Failed to save template.",
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteTemplate(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      toast.success("Template deleted.");
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : "Failed to delete template.",
      );
    },
  });

  function handleSubmitNew() {
    const name = editForm.name.trim();
    if (!name) return;
    saveMutation.mutate({
      name,
      description: editForm.description.trim(),
      system_prompt: editForm.system_prompt.trim(),
      sections: editForm.sections
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    });
  }

  function handleSubmitEdit(original: SummaryTemplate) {
    saveMutation.mutate({
      name: original.name,
      description: editForm.description.trim(),
      system_prompt: editForm.system_prompt.trim(),
      sections: editForm.sections
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    });
  }

  function openEdit(template: SummaryTemplate) {
    if (expandedTemplate === template.name) {
      setExpandedTemplate(null);
      return;
    }
    setExpandedTemplate(template.name);
    setCreating(false);
    setEditForm({
      name: template.name,
      description: template.description,
      system_prompt: template.system_prompt,
      sections: template.sections.join(", "),
    });
  }

  const FORM_INPUT =
    "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:border-accent";

  return (
    <Section
      title="Summary Templates"
      description="Manage templates for meeting summarisation"
    >
      <div className="py-3">
        {isLoading ? (
          <p className="text-sm text-text-muted">Loading templates...</p>
        ) : (
          <div className="grid grid-cols-1 gap-3">
            {templates.map((template) => {
              const isBuiltIn = BUILT_IN_TEMPLATES.includes(template.name);
              const isExpanded = expandedTemplate === template.name;

              return (
                <div
                  key={template.name}
                  className="rounded-lg bg-surface border border-border p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-text-primary">
                          {template.name}
                        </span>
                        {isBuiltIn && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-accent/10 text-accent">
                            Built-in
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-text-muted mt-0.5">
                        {template.description || "No description"}
                      </p>
                      <p className="text-xs text-text-muted mt-0.5">
                        {template.sections.length} section
                        {template.sections.length !== 1 ? "s" : ""}
                      </p>
                    </div>
                    {!isBuiltIn && (
                      <div className="flex items-center gap-2 shrink-0">
                        <button
                          onClick={() => openEdit(template)}
                          className="text-xs px-2 py-1 rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
                        >
                          {isExpanded ? "Collapse" : "Edit"}
                        </button>
                        <button
                          onClick={() => deleteMutation.mutate(template.name)}
                          disabled={deleteMutation.isPending}
                          className="text-xs px-2 py-1 rounded-lg text-status-error hover:bg-status-error/10 transition-colors"
                        >
                          Delete
                        </button>
                      </div>
                    )}
                  </div>

                  {/* Inline edit form for custom templates */}
                  {isExpanded && !isBuiltIn && (
                    <div className="mt-3 pt-3 border-t border-border flex flex-col gap-3">
                      <div>
                        <label className="text-xs text-text-muted">
                          Description
                        </label>
                        <input
                          type="text"
                          value={editForm.description}
                          onChange={(e) =>
                            setEditForm((f) => ({
                              ...f,
                              description: e.target.value,
                            }))
                          }
                          className={FORM_INPUT}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-text-muted">
                          System prompt
                        </label>
                        <textarea
                          value={editForm.system_prompt}
                          onChange={(e) =>
                            setEditForm((f) => ({
                              ...f,
                              system_prompt: e.target.value,
                            }))
                          }
                          rows={4}
                          className={`${FORM_INPUT} resize-y`}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-text-muted">
                          Sections (comma-separated)
                        </label>
                        <input
                          type="text"
                          value={editForm.sections}
                          onChange={(e) =>
                            setEditForm((f) => ({
                              ...f,
                              sections: e.target.value,
                            }))
                          }
                          className={FORM_INPUT}
                        />
                      </div>
                      <div className="flex gap-2 justify-end">
                        <button
                          onClick={() => setExpandedTemplate(null)}
                          className="px-3 py-1 text-xs rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => handleSubmitEdit(template)}
                          disabled={saveMutation.isPending}
                          className="px-3 py-1 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
                        >
                          {saveMutation.isPending ? "Saving..." : "Save"}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Create template form */}
        {creating ? (
          <div className="mt-3 rounded-lg bg-surface border border-border p-4 flex flex-col gap-3">
            <h3 className="text-sm font-medium text-text-primary">
              Create Template
            </h3>
            <div>
              <label className="text-xs text-text-muted">Name</label>
              <input
                type="text"
                value={editForm.name}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, name: e.target.value }))
                }
                placeholder="my-template"
                className={FORM_INPUT}
              />
            </div>
            <div>
              <label className="text-xs text-text-muted">Description</label>
              <input
                type="text"
                value={editForm.description}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, description: e.target.value }))
                }
                placeholder="What this template is for"
                className={FORM_INPUT}
              />
            </div>
            <div>
              <label className="text-xs text-text-muted">System prompt</label>
              <textarea
                value={editForm.system_prompt}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, system_prompt: e.target.value }))
                }
                rows={4}
                placeholder="Instructions for the summariser..."
                className={`${FORM_INPUT} resize-y`}
              />
            </div>
            <div>
              <label className="text-xs text-text-muted">
                Sections (comma-separated)
              </label>
              <input
                type="text"
                value={editForm.sections}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, sections: e.target.value }))
                }
                placeholder="Summary, Action Items, Decisions"
                className={FORM_INPUT}
              />
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => {
                  setCreating(false);
                  setEditForm({
                    name: "",
                    description: "",
                    system_prompt: "",
                    sections: "",
                  });
                }}
                className="px-3 py-1 text-xs rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSubmitNew}
                disabled={!editForm.name.trim() || saveMutation.isPending}
                className="px-3 py-1 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
              >
                {saveMutation.isPending ? "Saving..." : "Create"}
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={() => {
              setCreating(true);
              setExpandedTemplate(null);
              setEditForm({
                name: "",
                description: "",
                system_prompt: "",
                sections: "",
              });
            }}
            className="mt-3 px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
          >
            Create Template
          </button>
        )}
      </div>
    </Section>
  );
}

const INPUT =
  "bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:border-accent";
const NUM = `${INPUT} w-24 text-right`;
const TEXT = `${INPUT} w-48`;
const WIDE = `${INPUT} w-64`;
const SELECT = `${INPUT} appearance-none cursor-pointer pr-7`;

/* ------------------------------------------------------------------ */
/*  Main component                                                    */
/* ------------------------------------------------------------------ */

export function Settings() {
  const { daemonRunning, state } = useDaemonStatus();
  const wsConnected = useAppStore((s) => s.wsConnected);
  const queryClient = useQueryClient();
  const { theme, setTheme: applyTheme } = useTheme();
  const toast = useToast();

  const { data: fetchedConfig, isLoading: configLoading } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
    enabled: daemonRunning,
  });

  const [form, setForm] = useState<AppConfig | null>(null);
  const [savedConfig, setSavedConfig] = useState<AppConfig | null>(null);
  const [showRestart, setShowRestart] = useState(false);
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});

  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: getModels,
    enabled: daemonRunning,
    refetchInterval: 30_000,
  });

  const downloadMutation = useMutation({
    mutationFn: (name: string) => downloadModel(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });

  useEffect(() => {
    if (fetchedConfig && !form) {
      setForm(fetchedConfig);
      setSavedConfig(fetchedConfig);
    }
  }, [fetchedConfig, form]);

  const saveMutation = useMutation({
    mutationFn: (config: Partial<AppConfig>) => updateConfig(config),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      setForm(data);
      setSavedConfig(data);
      setShowRestart(true);
      toast.success("Settings saved successfully.");
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : "Failed to save settings.",
      );
    },
  });

  const isDirty = useMemo(
    () =>
      form !== null &&
      savedConfig !== null &&
      JSON.stringify(form) !== JSON.stringify(savedConfig),
    [form, savedConfig],
  );

  const discardChanges = useCallback(() => {
    if (savedConfig) setForm(savedConfig);
  }, [savedConfig]);

  // Block navigation when there are unsaved changes.
  const blocker = useBlocker(isDirty);

  function set<
    S extends keyof AppConfig,
    K extends string & keyof AppConfig[S],
  >(section: S, key: K, value: AppConfig[S][K]) {
    setForm((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        [section]: { ...prev[section], [key]: value },
      };
    });
  }

  function setNotionProp(key: string, value: string) {
    setForm((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        notion: {
          ...prev.notion,
          properties: { ...prev.notion.properties, [key]: value },
        },
      };
    });
  }

  const toggleSecret = (key: string) =>
    setShowSecrets((prev) => ({ ...prev, [key]: !prev[key] }));

  return (
    <div
      className="flex flex-col gap-4 p-6 max-w-3xl"
      role="form"
      aria-label="Application settings"
    >
      {/* Navigation blocker dialog */}
      {blocker.state === "blocked" && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <div className="rounded-xl bg-surface-raised border border-border p-6 max-w-sm shadow-lg">
            <h2 className="text-sm font-medium text-text-primary mb-2">
              Discard unsaved changes?
            </h2>
            <p className="text-xs text-text-muted mb-4">
              You have unsaved settings changes. Are you sure you want to leave?
            </p>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => blocker.reset()}
                className="px-3 py-1.5 text-xs rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
              >
                Stay
              </button>
              <button
                onClick={() => blocker.proceed()}
                className="px-3 py-1.5 text-xs rounded-lg bg-status-error text-white hover:opacity-90 transition-colors"
              >
                Discard & Leave
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-text-primary">Settings</h1>
        {form && (
          <button
            onClick={() => form && saveMutation.mutate(form)}
            disabled={!isDirty || saveMutation.isPending}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              isDirty
                ? "bg-accent text-white hover:bg-accent-hover"
                : "bg-surface-raised text-text-muted border border-border cursor-not-allowed"
            }`}
          >
            {saveMutation.isPending ? "Saving..." : "Save Changes"}
          </button>
        )}
      </div>

      {/* Unsaved changes sticky banner */}
      {isDirty && (
        <div className="sticky top-0 z-10 rounded-lg bg-accent/10 border border-accent/30 px-4 py-3 flex items-center justify-between">
          <p className="text-sm text-accent">You have unsaved changes</p>
          <div className="flex gap-2">
            <button
              onClick={discardChanges}
              className="px-3 py-1 text-xs rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
            >
              Discard
            </button>
            <button
              onClick={() => form && saveMutation.mutate(form)}
              disabled={saveMutation.isPending}
              className="px-3 py-1 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
            >
              {saveMutation.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      )}

      {/* Restart banner */}
      {showRestart && (
        <div className="rounded-lg bg-amber-400/10 border border-amber-400/30 px-4 py-3 flex items-center justify-between">
          <p className="text-sm text-amber-300">
            Settings saved. Restart the daemon for changes to take effect.
          </p>
          <button
            onClick={() => setShowRestart(false)}
            className="text-amber-300/60 hover:text-amber-300 text-sm ml-4"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Error banner */}
      {saveMutation.isError && (
        <div className="rounded-lg bg-status-error/10 border border-status-error/30 px-4 py-3">
          <p className="text-sm text-status-error">
            Failed to save:{" "}
            {saveMutation.error instanceof Error
              ? saveMutation.error.message
              : "An unexpected error occurred"}
          </p>
        </div>
      )}

      {/* Appearance — always visible, not dependent on daemon */}
      <Section title="Appearance" description="Theme and display preferences">
        <Field
          label="Theme"
          help="Choose light, dark, or follow system preference"
        >
          <select
            value={theme}
            onChange={(e) => applyTheme(e.target.value as Theme)}
            className={SELECT}
          >
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </Field>
      </Section>

      {/* Config form */}
      {!daemonRunning ? (
        <Section title="Configuration">
          <div className="py-3">
            <p className="text-sm text-text-secondary">
              Start the daemon to manage settings.
            </p>
            <p className="text-xs text-text-muted mt-2">
              You can also edit{" "}
              <code className="px-1.5 py-0.5 rounded bg-surface border border-border font-mono text-xs">
                config.yaml
              </code>{" "}
              directly.
            </p>
          </div>
        </Section>
      ) : configLoading || !form ? (
        <Section title="Configuration">
          <div className="flex items-center gap-2.5 py-6 justify-center">
            <svg
              className="animate-spin h-4 w-4 text-text-muted"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="3"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
            <span className="text-sm text-text-muted">
              Loading configuration...
            </span>
          </div>
        </Section>
      ) : (
        <>
          {/* Detection */}
          <Section
            title="Meeting Detection"
            description="When and how meetings are detected"
          >
            <Field
              label="Poll interval"
              help="How often to check for an active meeting"
            >
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={60}
                  value={form.detection.poll_interval_seconds}
                  onChange={(e) =>
                    set(
                      "detection",
                      "poll_interval_seconds",
                      Number(e.target.value),
                    )
                  }
                  className={NUM}
                />
                <span className="text-xs text-text-muted">sec</span>
              </div>
            </Field>
            <Field
              label="Minimum duration"
              help="Ignore meetings shorter than this"
            >
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={600}
                  value={form.detection.min_meeting_duration_seconds}
                  onChange={(e) =>
                    set(
                      "detection",
                      "min_meeting_duration_seconds",
                      Number(e.target.value),
                    )
                  }
                  className={NUM}
                />
                <span className="text-xs text-text-muted">sec</span>
              </div>
            </Field>
            <Field
              label="Consecutive detections"
              help="Positive polls required before recording starts"
            >
              <input
                type="number"
                min={1}
                max={10}
                value={form.detection.required_consecutive_detections}
                onChange={(e) =>
                  set(
                    "detection",
                    "required_consecutive_detections",
                    Number(e.target.value),
                  )
                }
                className={NUM}
              />
            </Field>
            <Field
              label="Process names"
              help="Comma-separated names to monitor"
            >
              <input
                type="text"
                value={(form.detection.process_names ?? []).join(", ")}
                onChange={(e) =>
                  set(
                    "detection",
                    "process_names",
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  )
                }
                className={WIDE}
              />
            </Field>
          </Section>

          {/* Audio */}
          <Section
            title="Audio Capture"
            description="Audio device and recording settings"
          >
            <Field
              label="BlackHole device"
              help="Virtual audio device name from Audio MIDI Setup"
            >
              <input
                type="text"
                value={form.audio.blackhole_device_name}
                onChange={(e) =>
                  set("audio", "blackhole_device_name", e.target.value)
                }
                className={TEXT}
              />
            </Field>
            <Field
              label="Microphone"
              help="Device name substring, empty for system default"
            >
              <input
                type="text"
                value={form.audio.mic_device_name}
                onChange={(e) =>
                  set("audio", "mic_device_name", e.target.value)
                }
                placeholder="System default"
                className={TEXT}
              />
            </Field>
            <Field
              label="Enable microphone"
              help="Mix mic input with system audio"
            >
              <Toggle
                checked={form.audio.mic_enabled}
                onChange={(v) => set("audio", "mic_enabled", v)}
                label="Enable microphone"
              />
            </Field>
            <Field label="Mic volume" help="0.0 to 2.0">
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={form.audio.mic_volume}
                onChange={(e) =>
                  set("audio", "mic_volume", Number(e.target.value))
                }
                className={NUM}
              />
            </Field>
            <Field label="System volume" help="0.0 to 2.0">
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={form.audio.system_volume}
                onChange={(e) =>
                  set("audio", "system_volume", Number(e.target.value))
                }
                className={NUM}
              />
            </Field>
            <Field label="Sample rate" help="Hz (16000 optimal for speech)">
              <select
                value={form.audio.sample_rate}
                onChange={(e) =>
                  set("audio", "sample_rate", Number(e.target.value))
                }
                className={SELECT}
              >
                <option value={16000}>16,000</option>
                <option value={44100}>44,100</option>
                <option value={48000}>48,000</option>
              </select>
            </Field>
            <Field
              label="Keep source files"
              help="Retain separate system/mic WAVs after merging"
            >
              <Toggle
                checked={form.audio.keep_source_files}
                onChange={(v) => set("audio", "keep_source_files", v)}
                label="Keep source files"
              />
            </Field>
          </Section>

          {/* Transcription */}
          <Section
            title="Transcription"
            description="Whisper model and inference settings"
          >
            <Field
              label="Model"
              help="MLX Whisper models run on Apple Silicon GPU"
            >
              <select
                value={form.transcription.model_size}
                onChange={(e) =>
                  set("transcription", "model_size", e.target.value)
                }
                className={SELECT}
              >
                <option value="mlx-community/whisper-large-v3-turbo">
                  large-v3-turbo (MLX, recommended)
                </option>
                <option value="mlx-community/whisper-large-v3">
                  large-v3 (MLX)
                </option>
                <option value="mlx-community/whisper-medium.en-mlx">
                  medium.en (MLX)
                </option>
                <option value="mlx-community/whisper-small.en-mlx">
                  small.en (MLX)
                </option>
              </select>
            </Field>
            <Field label="Language">
              <select
                value={form.transcription.language}
                onChange={(e) =>
                  set("transcription", "language", e.target.value)
                }
                className={SELECT}
              >
                <option value="en">English</option>
                <option value="auto">Auto-detect</option>
              </select>
            </Field>
            <Field
              label="VAD threshold"
              help="0.0–1.0 (lower keeps more audio)"
            >
              <Tooltip content="Voice Activity Detection sensitivity. Higher values require louder speech to trigger.">
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  id="vad-threshold"
                  value={form.transcription.vad_threshold}
                  onChange={(e) =>
                    set(
                      "transcription",
                      "vad_threshold",
                      Number(e.target.value),
                    )
                  }
                  className={NUM}
                />
              </Tooltip>
            </Field>
          </Section>

          {/* Whisper Models */}
          {modelsData && (
            <ModelSection
              models={modelsData.models}
              onDownload={(name) => downloadMutation.mutate(name)}
              downloading={downloadMutation.isPending}
            />
          )}

          {/* Summarisation */}
          <Section
            title="Summarisation"
            description="AI backend for generating meeting summaries"
          >
            <Field label="Backend">
              <select
                value={form.summarisation.backend}
                onChange={(e) =>
                  set(
                    "summarisation",
                    "backend",
                    e.target.value as "ollama" | "claude",
                  )
                }
                className={SELECT}
              >
                <option value="ollama">Ollama (local)</option>
                <option value="claude">Claude (API)</option>
              </select>
            </Field>

            {form.summarisation.backend === "ollama" ? (
              <>
                <Field label="Ollama URL" help="Base URL of the Ollama server">
                  <input
                    type="text"
                    value={form.summarisation.ollama_base_url}
                    onChange={(e) =>
                      set("summarisation", "ollama_base_url", e.target.value)
                    }
                    className={WIDE}
                  />
                </Field>
                <Field label="Ollama model">
                  <input
                    type="text"
                    value={form.summarisation.ollama_model}
                    onChange={(e) =>
                      set("summarisation", "ollama_model", e.target.value)
                    }
                    className={TEXT}
                  />
                </Field>
              </>
            ) : (
              <>
                <Field label="API key">
                  <div className="flex items-center gap-2">
                    <input
                      type={showSecrets["anthropic"] ? "text" : "password"}
                      value={form.summarisation.anthropic_api_key}
                      onChange={(e) =>
                        set(
                          "summarisation",
                          "anthropic_api_key",
                          e.target.value,
                        )
                      }
                      className={TEXT}
                    />
                    <button
                      type="button"
                      onClick={() => toggleSecret("anthropic")}
                      aria-pressed={!!showSecrets["anthropic"]}
                      aria-label={
                        showSecrets["anthropic"]
                          ? "Hide API key"
                          : "Show API key"
                      }
                      className="text-xs text-text-muted hover:text-text-secondary"
                    >
                      {showSecrets["anthropic"] ? "Hide" : "Show"}
                    </button>
                  </div>
                </Field>
                <Field label="Model" help="Claude model ID">
                  <input
                    type="text"
                    value={form.summarisation.model}
                    onChange={(e) =>
                      set("summarisation", "model", e.target.value)
                    }
                    className={TEXT}
                  />
                </Field>
              </>
            )}

            <Field label="Max tokens" help="Maximum summary response length">
              <input
                type="number"
                min={256}
                max={16384}
                step={256}
                value={form.summarisation.max_tokens}
                onChange={(e) =>
                  set("summarisation", "max_tokens", Number(e.target.value))
                }
                className={NUM}
              />
            </Field>
          </Section>

          {/* Diarisation */}
          <Section
            title="Speaker Diarisation"
            description="Energy-based speaker identification"
          >
            <Field
              label="Enabled"
              help="Requires microphone enabled for dual-source recording"
            >
              <Toggle
                checked={form.diarisation.enabled}
                onChange={(v) => set("diarisation", "enabled", v)}
                label="Enable diarisation"
              />
            </Field>
            {form.diarisation.enabled && (
              <>
                <Field label="Your name" help="Label for the local speaker">
                  <input
                    type="text"
                    value={form.diarisation.speaker_name}
                    onChange={(e) =>
                      set("diarisation", "speaker_name", e.target.value)
                    }
                    className={TEXT}
                  />
                </Field>
                <Field label="Remote label">
                  <input
                    type="text"
                    value={form.diarisation.remote_label}
                    onChange={(e) =>
                      set("diarisation", "remote_label", e.target.value)
                    }
                    className={TEXT}
                  />
                </Field>
                <Field
                  label="Energy threshold"
                  help="How much louder one source must be (0.1–5.0)"
                >
                  <Tooltip content="Minimum energy ratio between sources to distinguish speakers.">
                    <input
                      type="number"
                      min={0.1}
                      max={5}
                      step={0.1}
                      id="energy-threshold"
                      value={form.diarisation.energy_ratio_threshold}
                      onChange={(e) =>
                        set(
                          "diarisation",
                          "energy_ratio_threshold",
                          Number(e.target.value),
                        )
                      }
                      className={NUM}
                    />
                  </Tooltip>
                </Field>
              </>
            )}
          </Section>

          {/* Markdown output */}
          <Section
            title="Markdown Output"
            description="Write summaries to an Obsidian vault"
          >
            <Field label="Enabled">
              <Toggle
                checked={form.markdown.enabled}
                onChange={(v) => set("markdown", "enabled", v)}
                label="Enable markdown output"
              />
            </Field>
            {form.markdown.enabled && (
              <>
                <Field
                  label="Vault path"
                  help="Absolute path to the output folder"
                >
                  <input
                    type="text"
                    value={form.markdown.vault_path}
                    onChange={(e) =>
                      set("markdown", "vault_path", e.target.value)
                    }
                    className={WIDE}
                  />
                </Field>
                <Field
                  label="Filename template"
                  help="Variables: {date}, {time}, {slug}"
                >
                  <input
                    type="text"
                    value={form.markdown.filename_template}
                    onChange={(e) =>
                      set("markdown", "filename_template", e.target.value)
                    }
                    className={TEXT}
                  />
                </Field>
                <Field label="Include transcript">
                  <Toggle
                    checked={form.markdown.include_full_transcript}
                    label="Include full transcript"
                    onChange={(v) =>
                      set("markdown", "include_full_transcript", v)
                    }
                  />
                </Field>
              </>
            )}
          </Section>

          {/* Notion output */}
          <Section
            title="Notion Output"
            description="Create meeting pages in a Notion database"
          >
            <Field label="Enabled">
              <Toggle
                checked={form.notion.enabled}
                onChange={(v) => set("notion", "enabled", v)}
                label="Enable Notion output"
              />
            </Field>
            {form.notion.enabled && (
              <>
                <Field label="API key">
                  <div className="flex items-center gap-2">
                    <input
                      type={showSecrets["notion"] ? "text" : "password"}
                      value={form.notion.api_key}
                      onChange={(e) => set("notion", "api_key", e.target.value)}
                      className={TEXT}
                    />
                    <button
                      type="button"
                      onClick={() => toggleSecret("notion")}
                      aria-pressed={!!showSecrets["notion"]}
                      aria-label={
                        showSecrets["notion"] ? "Hide API key" : "Show API key"
                      }
                      className="text-xs text-text-muted hover:text-text-secondary"
                    >
                      {showSecrets["notion"] ? "Hide" : "Show"}
                    </button>
                  </div>
                </Field>
                <Field label="Database ID" help="From the Notion database URL">
                  <input
                    type="text"
                    value={form.notion.database_id}
                    onChange={(e) =>
                      set("notion", "database_id", e.target.value)
                    }
                    className={WIDE}
                  />
                </Field>
                <Field label="Title property">
                  <input
                    type="text"
                    value={form.notion.properties?.title ?? "Name"}
                    onChange={(e) => setNotionProp("title", e.target.value)}
                    className={TEXT}
                  />
                </Field>
                <Field label="Date property">
                  <input
                    type="text"
                    value={form.notion.properties?.date ?? "Date"}
                    onChange={(e) => setNotionProp("date", e.target.value)}
                    className={TEXT}
                  />
                </Field>
                <Field label="Tags property">
                  <input
                    type="text"
                    value={form.notion.properties?.tags ?? "Tags"}
                    onChange={(e) => setNotionProp("tags", e.target.value)}
                    className={TEXT}
                  />
                </Field>
                <Field label="Status property">
                  <input
                    type="text"
                    value={form.notion.properties?.status ?? "Status"}
                    onChange={(e) => setNotionProp("status", e.target.value)}
                    className={TEXT}
                  />
                </Field>
              </>
            )}
          </Section>

          {/* Data Retention */}
          <Section
            title="Data Retention"
            description="Automatically clean up old data. Set to 0 to keep forever."
          >
            <Field
              label="Delete audio after"
              help="Remove audio files after this many days (keeps meeting record)"
            >
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={3650}
                  value={form.retention.audio_retention_days}
                  onChange={(e) =>
                    set(
                      "retention",
                      "audio_retention_days",
                      Number(e.target.value),
                    )
                  }
                  className={NUM}
                />
                <span className="text-xs text-text-muted">days</span>
              </div>
            </Field>
            <Field
              label="Delete records after"
              help="Remove entire meeting records (including audio) after this many days"
            >
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={3650}
                  value={form.retention.record_retention_days}
                  onChange={(e) =>
                    set(
                      "retention",
                      "record_retention_days",
                      Number(e.target.value),
                    )
                  }
                  className={NUM}
                />
                <span className="text-xs text-text-muted">days</span>
              </div>
            </Field>
          </Section>

          {/* Logging */}
          <Section title="Logging">
            <Field label="Log level">
              <select
                value={form.logging.level}
                onChange={(e) => set("logging", "level", e.target.value)}
                className={SELECT}
              >
                <option value="DEBUG">DEBUG</option>
                <option value="INFO">INFO</option>
                <option value="WARNING">WARNING</option>
                <option value="ERROR">ERROR</option>
              </select>
            </Field>
            <Field label="Log file">
              <input
                type="text"
                value={form.logging.log_file}
                onChange={(e) => set("logging", "log_file", e.target.value)}
                className={WIDE}
              />
            </Field>
          </Section>
        </>
      )}

      {/* Summary Templates — always visible when daemon is running */}
      {daemonRunning && <TemplatesSection />}

      {/* Read-only info */}
      <Section title="Daemon">
        <InfoRow label="Status" value={daemonRunning ? "Running" : "Offline"} />
        <InfoRow label="State" value={state} />
        <InfoRow
          label="WebSocket"
          value={wsConnected ? "Connected" : "Disconnected"}
        />
        <InfoRow label="API" value="http://127.0.0.1:9876" />
      </Section>

      <Section title="About">
        <InfoRow label="App" value="MeetingMind" />
        <InfoRow label="Version" value="0.1.0" />
        <InfoRow label="Platform" value="macOS (Tauri)" />
        <UpdateChecker />
      </Section>
    </div>
  );
}
