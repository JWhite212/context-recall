import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import Markdown from "react-markdown";
import {
  getUpcomingPrepList,
  getPrepForMeeting,
  generatePrep,
} from "../../lib/api";

function UpcomingList() {
  const { data: briefings = [], isLoading } = useQuery({
    queryKey: ["prep", "upcoming-list"],
    queryFn: () => getUpcomingPrepList(),
  });

  if (isLoading) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="h-4 w-5/6 bg-surface border border-border rounded animate-pulse" />
      </div>
    );
  }
  if (briefings.length === 0) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <p className="text-sm text-text-muted text-center py-16">
          No upcoming briefings
        </p>
      </div>
    );
  }
  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      {briefings.map((b) => (
        <div
          key={b.id}
          className="rounded-xl border border-border bg-surface-raised p-5"
        >
          <div className="prose prose-sm prose-invert max-w-none [&_h1]:text-base [&_h2]:text-sm [&_h2]:mt-4 [&_li]:text-text-secondary [&_p]:text-text-secondary">
            <Markdown>{b.content_markdown}</Markdown>
          </div>
        </div>
      ))}
    </div>
  );
}

export function PrepBriefing() {
  const { meetingId } = useParams<{ meetingId: string }>();
  const queryClient = useQueryClient();

  const {
    data: briefing,
    isLoading,
    refetch,
  } = useQuery({
    queryKey: ["prep", meetingId],
    queryFn: () => getPrepForMeeting(meetingId!),
    enabled: !!meetingId,
  });

  const generate = useMutation({
    mutationFn: () => generatePrep(meetingId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prep", meetingId] });
      refetch();
    },
  });

  if (!meetingId) {
    return <UpcomingList />;
  }

  // Loading skeleton
  if (isLoading) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="h-6 w-48 bg-surface border border-border rounded animate-pulse mb-6" />
        <div className="space-y-3">
          <div className="h-4 w-full bg-surface border border-border rounded animate-pulse" />
          <div className="h-4 w-5/6 bg-surface border border-border rounded animate-pulse" />
          <div className="h-4 w-4/6 bg-surface border border-border rounded animate-pulse" />
          <div className="h-4 w-full bg-surface border border-border rounded animate-pulse" />
          <div className="h-4 w-3/4 bg-surface border border-border rounded animate-pulse" />
        </div>
      </div>
    );
  }

  // Empty state
  if (!briefing) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <p className="text-sm text-text-muted mb-4">
            No prep briefing available
          </p>
          <button
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
            className="px-4 py-2 text-sm bg-accent text-white rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
          >
            {generate.isPending ? "Generating..." : "Generate Briefing"}
          </button>
        </div>
      </div>
    );
  }

  // Content
  return (
    <div className="p-6 max-w-3xl mx-auto">
      <div className="prose prose-sm prose-invert max-w-none [&_h1]:text-base [&_h2]:text-sm [&_h2]:mt-4 [&_li]:text-text-secondary [&_p]:text-text-secondary">
        <Markdown>{briefing.content_markdown}</Markdown>
      </div>
    </div>
  );
}
