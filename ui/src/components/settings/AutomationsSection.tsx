import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getAutomationRules,
  createAutomationRule,
  updateAutomationRule,
  deleteAutomationRule,
} from "../../lib/api";
import type {
  AutomationRule,
  AutomationCondition,
  AutomationAction,
  AutomationConditionField,
  AutomationActionType,
} from "../../lib/types";
import { useToast } from "../common/Toast";

const FORM_INPUT =
  "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:border-accent";

const CONDITION_FIELDS: { value: AutomationConditionField; label: string }[] = [
  { value: "tag", label: "Tag is" },
  { value: "client", label: "Client is" },
  { value: "project", label: "Project is" },
  { value: "title_contains", label: "Title contains" },
  { value: "attendee_domain", label: "Attendee domain is" },
];

const ACTION_TYPES: {
  value: AutomationActionType;
  label: string;
  placeholder: string;
}[] = [
  {
    value: "apply_tag",
    label: "Apply tag(s)",
    placeholder: "tags, comma-separated",
  },
  { value: "webhook", label: "POST webhook", placeholder: "https://…" },
  { value: "notify", label: "Notify me", placeholder: "message (optional)" },
];

interface CondRow {
  field: AutomationConditionField;
  value: string;
}

interface ActionRow {
  type: AutomationActionType;
  value: string;
}

function buildAction(row: ActionRow): AutomationAction | null {
  if (row.type === "apply_tag") {
    const tags = row.value
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    return tags.length ? { type: "apply_tag", tags } : null;
  }
  if (row.type === "webhook") {
    const url = row.value.trim();
    return url ? { type: "webhook", url } : null;
  }
  const message = row.value.trim();
  return message ? { type: "notify", message } : { type: "notify" };
}

function conditionLabel(c: AutomationCondition): string {
  const field = CONDITION_FIELDS.find((f) => f.value === c.field);
  return `${field ? field.label : c.field} ${c.value}`;
}

function actionLabel(a: AutomationAction): string {
  if (a.type === "apply_tag") return `apply tag: ${(a.tags ?? []).join(", ")}`;
  if (a.type === "webhook") return `webhook → ${a.url}`;
  return a.message ? `notify: ${a.message}` : "notify";
}

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

/** Settings panel to manage automation rules (conditions → actions). */
export function AutomationsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [matchMode, setMatchMode] = useState<"all" | "any">("all");
  const [conds, setConds] = useState<CondRow[]>([{ field: "tag", value: "" }]);
  const [actions, setActions] = useState<ActionRow[]>([
    { type: "apply_tag", value: "" },
  ]);

  const { data: rules = [], isLoading } = useQuery({
    queryKey: ["automation-rules"],
    queryFn: getAutomationRules,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["automation-rules"] });

  const builtConditions: AutomationCondition[] = conds
    .filter((c) => c.value.trim())
    .map((c) => ({ field: c.field, value: c.value.trim() }));
  const builtActions = actions
    .map(buildAction)
    .filter((a): a is AutomationAction => a !== null);
  const canSubmit =
    name.trim().length > 0 &&
    builtConditions.length > 0 &&
    builtActions.length > 0;

  const create = useMutation({
    mutationFn: () =>
      createAutomationRule({
        name: name.trim(),
        match_mode: matchMode,
        conditions: builtConditions,
        actions: builtActions,
      }),
    onSuccess: () => {
      setName("");
      setMatchMode("all");
      setConds([{ field: "tag", value: "" }]);
      setActions([{ type: "apply_tag", value: "" }]);
      void invalidate();
    },
    onError: () => toast.error("Failed to create rule."),
  });

  const toggle = useMutation({
    mutationFn: (rule: AutomationRule) =>
      updateAutomationRule(rule.id, { enabled: !rule.enabled }),
    onSuccess: () => void invalidate(),
    onError: () => toast.error("Failed to update rule."),
  });

  const remove = useMutation({
    mutationFn: (ruleId: string) => deleteAutomationRule(ruleId),
    onSuccess: () => void invalidate(),
    onError: () => toast.error("Failed to delete rule."),
  });

  const setCond = (i: number, patch: Partial<CondRow>) =>
    setConds((rows) =>
      rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)),
    );
  const setAction = (i: number, patch: Partial<ActionRow>) =>
    setActions((rows) =>
      rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)),
    );

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Automations</legend>
      <h2 className="text-sm font-medium text-text-primary">Automations</h2>
      <p className="text-xs text-text-muted mt-1">
        When a meeting matches your conditions, run actions automatically.
      </p>

      <div className="py-3 flex flex-col gap-3">
        {isLoading ? (
          <p className="text-sm text-text-muted">Loading rules...</p>
        ) : rules.length === 0 ? (
          <p className="text-sm text-text-muted">No automation rules yet.</p>
        ) : (
          <div className="grid grid-cols-1 gap-2">
            {rules.map((rule) => (
              <div
                key={rule.id}
                className="rounded-lg bg-surface border border-border p-3 flex items-start justify-between gap-3"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-text-primary">
                    {rule.name}
                  </p>
                  <p className="text-xs text-text-muted mt-0.5">
                    Match {rule.match_mode === "any" ? "any" : "all"}:{" "}
                    {rule.conditions.map(conditionLabel).join(", ")}
                  </p>
                  <p className="text-xs text-text-muted mt-0.5 truncate">
                    → {rule.actions.map(actionLabel).join("; ")}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Toggle
                    checked={rule.enabled}
                    onChange={() => toggle.mutate(rule)}
                    label={`Enable ${rule.name}`}
                  />
                  <button
                    type="button"
                    onClick={() => remove.mutate(rule.id)}
                    className="text-xs text-status-error hover:underline"
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="rounded-lg bg-surface border border-border p-3 flex flex-col gap-2">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Rule name (e.g. Tag discovery meetings)"
            aria-label="Rule name"
            className={FORM_INPUT}
          />

          <label className="text-xs text-text-muted flex items-center gap-2">
            Match
            <select
              value={matchMode}
              onChange={(e) => setMatchMode(e.target.value as "all" | "any")}
              aria-label="Match mode"
              className={`${FORM_INPUT} w-auto`}
            >
              <option value="all">all conditions</option>
              <option value="any">any condition</option>
            </select>
          </label>

          <div className="flex flex-col gap-1.5">
            <p className="text-[10px] uppercase tracking-wide text-text-muted">
              Conditions
            </p>
            {conds.map((c, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <select
                  value={c.field}
                  onChange={(e) =>
                    setCond(i, {
                      field: e.target.value as AutomationConditionField,
                    })
                  }
                  aria-label="Condition field"
                  className={`${FORM_INPUT} w-auto`}
                >
                  {CONDITION_FIELDS.map((f) => (
                    <option key={f.value} value={f.value}>
                      {f.label}
                    </option>
                  ))}
                </select>
                <input
                  type="text"
                  value={c.value}
                  onChange={(e) => setCond(i, { value: e.target.value })}
                  placeholder="value"
                  aria-label="Condition value"
                  className={FORM_INPUT}
                />
                {conds.length > 1 && (
                  <button
                    type="button"
                    onClick={() =>
                      setConds((rows) => rows.filter((_, idx) => idx !== i))
                    }
                    aria-label="Remove condition"
                    className="text-xs text-status-error px-1"
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              onClick={() =>
                setConds((rows) => [...rows, { field: "tag", value: "" }])
              }
              className="self-start text-xs text-accent hover:underline"
            >
              + Add condition
            </button>
          </div>

          <div className="flex flex-col gap-1.5">
            <p className="text-[10px] uppercase tracking-wide text-text-muted">
              Actions
            </p>
            {actions.map((a, i) => {
              const meta = ACTION_TYPES.find((t) => t.value === a.type);
              return (
                <div key={i} className="flex items-center gap-1.5">
                  <select
                    value={a.type}
                    onChange={(e) =>
                      setAction(i, {
                        type: e.target.value as AutomationActionType,
                      })
                    }
                    aria-label="Action type"
                    className={`${FORM_INPUT} w-auto`}
                  >
                    {ACTION_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                  <input
                    type="text"
                    value={a.value}
                    onChange={(e) => setAction(i, { value: e.target.value })}
                    placeholder={meta?.placeholder}
                    aria-label="Action value"
                    className={FORM_INPUT}
                  />
                  {actions.length > 1 && (
                    <button
                      type="button"
                      onClick={() =>
                        setActions((rows) => rows.filter((_, idx) => idx !== i))
                      }
                      aria-label="Remove action"
                      className="text-xs text-status-error px-1"
                    >
                      ✕
                    </button>
                  )}
                </div>
              );
            })}
            <button
              type="button"
              onClick={() =>
                setActions((rows) => [
                  ...rows,
                  { type: "apply_tag", value: "" },
                ])
              }
              className="self-start text-xs text-accent hover:underline"
            >
              + Add action
            </button>
          </div>

          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={!canSubmit || create.isPending}
            className="self-end px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
          >
            Add rule
          </button>
        </div>
      </div>
    </fieldset>
  );
}
