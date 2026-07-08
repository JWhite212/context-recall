import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import Markdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import { askMeetings } from "../../lib/api";
import { useToast } from "../common/Toast";
import type { AskResponse } from "../../lib/types";

/**
 * Ask-your-meetings: a question box over the whole meeting history.
 * Retrieval is hybrid (semantic + keyword); the answer cites sources
 * that link back to the meetings.
 */
export function AskView() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<AskResponse | null>(null);
  const navigate = useNavigate();
  const toast = useToast();

  const ask = useMutation({
    mutationFn: (q: string) => askMeetings(q),
    onSuccess: setResult,
    onError: () =>
      toast.error("Could not answer — is the summarisation backend running?"),
  });

  const submit = () => {
    const q = question.trim();
    if (q.length >= 3 && !ask.isPending) {
      setResult(null);
      ask.mutate(q);
    }
  };

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      <h1 className="text-lg font-semibold text-text-primary">
        Ask your meetings
      </h1>
      <p className="text-xs text-text-muted">
        Ask anything discussed in your recorded meetings — decisions, dates, who
        said what. Answers cite the meetings they came from and never leave your
        machine unless your summarisation backend is Claude.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="flex gap-2"
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. What did we decide about the March launch?"
          className="flex-1 px-3 py-2 text-sm rounded-lg bg-surface-raised border border-border text-text-primary"
        />
        <button
          type="submit"
          disabled={question.trim().length < 3 || ask.isPending}
          className="text-sm px-4 py-2 rounded-lg bg-accent text-white disabled:opacity-50 cursor-pointer"
        >
          {ask.isPending ? "Thinking…" : "Ask"}
        </button>
      </form>

      {ask.isPending && (
        <p className="text-xs text-text-muted animate-pulse">
          Searching transcripts and composing an answer — local models can take
          a minute…
        </p>
      )}

      {result && result.no_results && (
        <p className="text-sm text-text-muted">
          Nothing in your meetings matches that question yet.
        </p>
      )}

      {result && !result.no_results && (
        <div className="flex flex-col gap-3">
          <div className="rounded-xl bg-surface-raised border border-border p-4 prose prose-invert prose-sm max-w-none">
            <Markdown rehypePlugins={[rehypeSanitize]}>
              {result.answer}
            </Markdown>
          </div>
          {result.sources.length > 0 && (
            <div className="rounded-xl bg-surface-raised border border-border p-3">
              <p className="text-[10px] uppercase tracking-wide text-text-muted px-2 pb-1">
                Sources
              </p>
              {result.sources.map((s) => (
                <button
                  key={`${s.index}-${s.meeting_id}`}
                  onClick={() => navigate(`/meetings/${s.meeting_id}`)}
                  className="w-full text-left flex items-start gap-2 py-1.5 px-2 rounded-lg hover:bg-sidebar-hover cursor-pointer"
                >
                  <span className="text-xs text-accent shrink-0">
                    [{s.index}]
                  </span>
                  <span className="min-w-0">
                    <span className="text-xs font-medium text-text-primary block truncate">
                      {s.title} —{" "}
                      {new Date(s.started_at * 1000).toLocaleDateString()}
                    </span>
                    <span className="text-xs text-text-muted line-clamp-2">
                      {s.snippet}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
