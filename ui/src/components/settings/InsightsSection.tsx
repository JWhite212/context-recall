import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getInsightDefinitions,
  createInsightDefinition,
  updateInsightDefinition,
  deleteInsightDefinition,
} from "../../lib/api";
import type { InsightDefinition, InsightFieldType } from "../../lib/types";
import { useToast } from "../common/Toast";

const FORM_INPUT =
  "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:border-accent";

/** The exact field-type vocabulary an insight's structured fields may use. */
const FIELD_TYPES: InsightFieldType[] = [
  "text",
  "number",
  "date",
  "boolean",
  "list",
];

/** A field row being edited before submit; `key` is derived from `label` on save. */
interface DraftField {
  label: string;
  type: InsightFieldType;
}

/** Slug a field's display label into a stable snake_case key. */
function slugifyFieldKey(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
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

/** Settings panel to manage custom insight definitions (name + prompt + enabled). */
export function InsightsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [outputMode, setOutputMode] =
    useState<InsightDefinition["output_mode"]>("list");
  const [fields, setFields] = useState<DraftField[]>([]);

  const { data: definitions = [], isLoading } = useQuery({
    queryKey: ["insight-definitions"],
    queryFn: getInsightDefinitions,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["insight-definitions"] });

  const addField = () =>
    setFields((prev) => [...prev, { label: "", type: "text" }]);

  const updateField = (index: number, patch: Partial<DraftField>) =>
    setFields((prev) =>
      prev.map((field, i) => (i === index ? { ...field, ...patch } : field)),
    );

  const removeField = (index: number) =>
    setFields((prev) => prev.filter((_, i) => i !== index));

  const create = useMutation({
    mutationFn: () => {
      const base = { name: name.trim(), prompt: prompt.trim() };
      if (outputMode === "structured") {
        return createInsightDefinition({
          ...base,
          output_mode: "structured",
          fields: fields.map((field) => ({
            key: slugifyFieldKey(field.label),
            label: field.label,
            type: field.type,
          })),
        });
      }
      return createInsightDefinition(base);
    },
    onSuccess: () => {
      setName("");
      setPrompt("");
      setOutputMode("list");
      setFields([]);
      void invalidate();
    },
    onError: () => toast.error("Failed to create insight."),
  });

  const toggle = useMutation({
    mutationFn: (def: InsightDefinition) =>
      updateInsightDefinition(def.id, { enabled: !def.enabled }),
    onSuccess: () => void invalidate(),
    onError: () => toast.error("Failed to update insight."),
  });

  const remove = useMutation({
    mutationFn: (defId: string) => deleteInsightDefinition(defId),
    onSuccess: () => void invalidate(),
    onError: () => toast.error("Failed to delete insight."),
  });

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Custom Insights</legend>
      <h2 className="text-sm font-medium text-text-primary">Custom Insights</h2>
      <p className="text-xs text-text-muted mt-1">
        Define what to extract from each meeting (e.g. Risks, Decisions).
      </p>

      <div className="py-3 flex flex-col gap-3">
        {isLoading ? (
          <p className="text-sm text-text-muted">Loading insights...</p>
        ) : definitions.length === 0 ? (
          <p className="text-sm text-text-muted">No insights defined yet.</p>
        ) : (
          <div className="grid grid-cols-1 gap-2">
            {definitions.map((def) => (
              <div
                key={def.id}
                className="rounded-lg bg-surface border border-border p-3 flex items-start justify-between gap-3"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-text-primary">
                    {def.name}
                  </p>
                  <p className="text-xs text-text-muted mt-0.5 truncate">
                    {def.prompt}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Toggle
                    checked={def.enabled}
                    onChange={() => toggle.mutate(def)}
                    label={`Enable ${def.name}`}
                  />
                  <button
                    type="button"
                    onClick={() => remove.mutate(def.id)}
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
            placeholder="Insight name (e.g. Risks)"
            aria-label="Insight name"
            className={FORM_INPUT}
          />
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What should the AI extract? (e.g. List the risks raised.)"
            aria-label="Insight prompt"
            rows={2}
            className={FORM_INPUT}
          />

          <div
            role="radiogroup"
            aria-label="Output mode"
            className="flex items-center gap-4 text-xs text-text-muted"
          >
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                name="insight-output-mode"
                value="list"
                checked={outputMode === "list"}
                onChange={() => setOutputMode("list")}
              />
              List
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                name="insight-output-mode"
                value="structured"
                checked={outputMode === "structured"}
                onChange={() => setOutputMode("structured")}
              />
              Structured
            </label>
          </div>

          {outputMode === "structured" && (
            <div className="flex flex-col gap-2">
              {fields.map((field, index) => (
                <div key={index} className="flex items-center gap-2">
                  <input
                    type="text"
                    value={field.label}
                    onChange={(e) =>
                      updateField(index, { label: e.target.value })
                    }
                    placeholder="Field label"
                    aria-label="Field label"
                    className={FORM_INPUT}
                  />
                  <select
                    value={field.type}
                    onChange={(e) =>
                      updateField(index, {
                        type: e.target.value as InsightFieldType,
                      })
                    }
                    aria-label="Field type"
                    className={FORM_INPUT}
                  >
                    {FIELD_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => removeField(index)}
                    aria-label={`Remove field ${index + 1}`}
                    className="shrink-0 text-xs text-status-error hover:underline"
                  >
                    Remove
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addField}
                className="self-start px-3 py-1.5 text-xs rounded-lg border border-border text-text-primary hover:bg-surface-raised transition-colors"
              >
                Add field
              </button>
            </div>
          )}

          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={!name.trim() || !prompt.trim() || create.isPending}
            className="self-end px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
          >
            Add insight
          </button>
        </div>
      </div>
    </fieldset>
  );
}
