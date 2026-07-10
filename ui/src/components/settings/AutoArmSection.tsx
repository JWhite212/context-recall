import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getConfig, updateConfig } from "../../lib/api";
import { useToast } from "../common/Toast";

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

/** Settings panel: master switch for calendar auto-arm recording. */
export function AutoArmSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });

  const enabled = config?.auto_arm?.enabled ?? false;

  const save = useMutation({
    mutationFn: (next: boolean) =>
      updateConfig({ auto_arm: { enabled: next } }),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      toast.success("Auto-record setting saved.");
    },
    onError: () => toast.error("Failed to save auto-record setting."),
  });

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Auto-record</legend>
      <h2 className="text-sm font-medium text-text-primary">Auto-record</h2>
      <p className="text-xs text-text-muted mt-1">
        Automatically start recording scheduled meetings that have a join link.
        Requires calendar import.
      </p>

      <div className="py-3 flex items-center justify-between">
        <span className="text-sm text-text-secondary">
          Auto-record scheduled meetings
        </span>
        <Toggle
          checked={enabled}
          onChange={(v) => save.mutate(v)}
          label="Auto-record scheduled meetings"
        />
      </div>
    </fieldset>
  );
}
