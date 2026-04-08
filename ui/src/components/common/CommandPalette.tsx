import { Command } from "cmdk";
import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getMeetings, startRecording, stopRecording } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const navigate = useNavigate();
  const { state, daemonRunning } = useDaemonStatus();

  const { data: recentMeetings } = useQuery({
    queryKey: ["meetings", "palette"],
    queryFn: () => getMeetings(5, 0),
    enabled: open,
  });

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "k" && e.metaKey) {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const runAndClose = useCallback(
    (fn: () => void) => {
      fn();
      setOpen(false);
      setSearch("");
    },
    [],
  );

  const isRecording = state === "recording";

  return (
    <Command.Dialog
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        if (!value) setSearch("");
      }}
      label="Command palette"
      className="cmdk-dialog"
    >
      <Command.Input
        value={search}
        onValueChange={setSearch}
        placeholder="Type a command or search..."
        className="cmdk-input"
      />
      <Command.List className="cmdk-list">
        <Command.Empty className="cmdk-empty">No results found.</Command.Empty>

        <Command.Group heading="Navigation" className="cmdk-group">
          <Command.Item
            onSelect={() => runAndClose(() => navigate("/"))}
            className="cmdk-item"
          >
            <GridIcon />
            Dashboard
          </Command.Item>
          <Command.Item
            onSelect={() => runAndClose(() => navigate("/live"))}
            className="cmdk-item"
          >
            <MicIcon />
            Live View
          </Command.Item>
          <Command.Item
            onSelect={() => runAndClose(() => navigate("/meetings"))}
            className="cmdk-item"
          >
            <ListIcon />
            Meetings
          </Command.Item>
          <Command.Item
            onSelect={() => runAndClose(() => navigate("/settings"))}
            className="cmdk-item"
          >
            <SettingsIcon />
            Settings
          </Command.Item>
        </Command.Group>

        <Command.Group heading="Actions" className="cmdk-group">
          {isRecording ? (
            <Command.Item
              onSelect={() => runAndClose(() => { stopRecording(); })}
              className="cmdk-item"
            >
              <StopIcon />
              Stop Recording
            </Command.Item>
          ) : (
            <Command.Item
              disabled={!daemonRunning}
              onSelect={() =>
                runAndClose(() => {
                  startRecording();
                  navigate("/live");
                })
              }
              className="cmdk-item"
            >
              <RecordIcon />
              Start Recording
            </Command.Item>
          )}
          <Command.Item
            onSelect={() =>
              runAndClose(() => {
                if (search.trim()) {
                  navigate(`/meetings?q=${encodeURIComponent(search.trim())}`);
                } else {
                  navigate("/meetings");
                }
              })
            }
            className="cmdk-item"
          >
            <SearchIcon />
            {search.trim() ? `Search meetings for "${search.trim()}"` : "Search Meetings"}
          </Command.Item>
        </Command.Group>

        {recentMeetings && recentMeetings.meetings.length > 0 && (
          <Command.Group heading="Recent Meetings" className="cmdk-group">
            {recentMeetings.meetings.map((m) => (
              <Command.Item
                key={m.id}
                value={`meeting ${m.title}`}
                onSelect={() => runAndClose(() => navigate(`/meetings/${m.id}`))}
                className="cmdk-item"
              >
                <DocIcon />
                <span className="flex-1 truncate">{m.title}</span>
                <span className="text-text-muted text-xs">
                  {new Date(m.started_at * 1000).toLocaleDateString()}
                </span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
      </Command.List>
    </Command.Dialog>
  );
}

/* Inline SVG icons — 16x16, matching the project's icon style. */

function GridIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="cmdk-icon">
      <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="cmdk-icon">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" y1="19" x2="12" y2="23" /><line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

function ListIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="cmdk-icon">
      <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="cmdk-icon">
      <circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="cmdk-icon">
      <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

function RecordIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="cmdk-icon">
      <circle cx="12" cy="12" r="8" fill="currentColor" className="text-status-recording" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="cmdk-icon">
      <rect x="6" y="6" width="12" height="12" rx="1" fill="currentColor" className="text-status-recording" />
    </svg>
  );
}

function DocIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="cmdk-icon">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><polyline points="10 9 9 9 8 9" />
    </svg>
  );
}
