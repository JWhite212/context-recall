import { format } from "date-fns";
import type { Meeting } from "../../lib/types";
import { EventCard } from "./EventCard";

interface AgendaListProps {
  meetings: Meeting[];
}

export function AgendaList({ meetings }: AgendaListProps) {
  // Group meetings by date (newest first)
  const sorted = [...meetings].sort((a, b) => b.started_at - a.started_at);

  const groups: { date: string; meetings: Meeting[] }[] = [];
  let currentGroup: { date: string; meetings: Meeting[] } | null = null;

  for (const meeting of sorted) {
    const dateKey = format(new Date(meeting.started_at * 1000), "yyyy-MM-dd");
    if (!currentGroup || currentGroup.date !== dateKey) {
      currentGroup = { date: dateKey, meetings: [] };
      groups.push(currentGroup);
    }
    currentGroup.meetings.push(meeting);
  }

  if (groups.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-sm text-text-muted">No meetings in this period</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 p-4 overflow-auto gap-4">
      {groups.map((group) => (
        <div key={group.date}>
          <h3 className="text-xs font-medium text-text-muted mb-2 sticky top-0 bg-surface py-1">
            {format(new Date(group.date + "T00:00:00"), "EEEE, MMMM d")}
            <span className="ml-2 text-text-muted/60">
              ({group.meetings.length})
            </span>
          </h3>
          <div className="flex flex-col gap-1.5">
            {group.meetings.map((meeting) => (
              <div key={meeting.id} className="flex items-start gap-2">
                <span className="text-[11px] text-text-muted w-12 pt-3 text-right shrink-0">
                  {format(new Date(meeting.started_at * 1000), "HH:mm")}
                </span>
                <div className="flex-1">
                  <EventCard meeting={meeting} />
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
