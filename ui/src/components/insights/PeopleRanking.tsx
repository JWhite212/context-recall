import type { MostMetPerson } from "../../lib/types";

interface Props {
  people: MostMetPerson[];
}

export function PeopleRanking({ people }: Props) {
  if (people.length === 0) {
    return (
      <div className="p-4 bg-surface-raised border border-border rounded-lg">
        <p className="text-xs text-text-muted mb-2">Most Met People</p>
        <p className="text-sm text-text-muted">No data yet</p>
      </div>
    );
  }

  return (
    <div className="p-4 bg-surface-raised border border-border rounded-lg">
      <p className="text-xs text-text-muted mb-2">Most Met People</p>
      <ul className="space-y-1">
        {people.map((person, idx) => (
          <li key={idx} className="flex items-center gap-2 text-sm">
            <span className="text-text-muted w-4 text-right">{idx + 1}.</span>
            <span className="text-text-primary truncate flex-1">
              {person.name}
            </span>
            <span className="text-text-muted">{person.meeting_count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
