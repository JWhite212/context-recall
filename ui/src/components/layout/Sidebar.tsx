import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getMeetingStats } from "../../lib/api";
import { NotificationBadge } from "../notifications/NotificationBadge";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: "grid" },
  { to: "/live", label: "Live", icon: "mic" },
  { to: "/meetings", label: "Meetings", icon: "list" },
  { to: "/calendar", label: "Calendar", icon: "calendar" },
  { to: "/action-items", label: "Action Items", icon: "check-circle" },
  { to: "/series", label: "Series", icon: "layers" },
  { to: "/insights", label: "Insights", icon: "bar-chart" },
  { to: "/search", label: "Search", icon: "search" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

// Simple SVG icons to avoid a dependency.
function Icon({ name }: { name: string }) {
  switch (name) {
    case "grid":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="3" y="3" width="7" height="7" />
          <rect x="14" y="3" width="7" height="7" />
          <rect x="3" y="14" width="7" height="7" />
          <rect x="14" y="14" width="7" height="7" />
        </svg>
      );
    case "mic":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="23" />
          <line x1="8" y1="23" x2="16" y2="23" />
        </svg>
      );
    case "list":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <line x1="8" y1="6" x2="21" y2="6" />
          <line x1="8" y1="12" x2="21" y2="12" />
          <line x1="8" y1="18" x2="21" y2="18" />
          <line x1="3" y1="6" x2="3.01" y2="6" />
          <line x1="3" y1="12" x2="3.01" y2="12" />
          <line x1="3" y1="18" x2="3.01" y2="18" />
        </svg>
      );
    case "calendar":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
          <line x1="16" y1="2" x2="16" y2="6" />
          <line x1="8" y1="2" x2="8" y2="6" />
          <line x1="3" y1="10" x2="21" y2="10" />
        </svg>
      );
    case "search":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
      );
    case "settings":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      );
    case "check-circle":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
          <polyline points="22 4 12 14.01 9 11.01" />
        </svg>
      );
    case "layers":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polygon points="12 2 2 7 12 12 22 7 12 2" />
          <polyline points="2 17 12 22 22 17" />
          <polyline points="2 12 12 17 22 12" />
        </svg>
      );
    case "bar-chart":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <line x1="12" y1="20" x2="12" y2="10" />
          <line x1="18" y1="20" x2="18" y2="4" />
          <line x1="6" y1="20" x2="6" y2="16" />
        </svg>
      );
    case "bell":
      return (
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
      );
    default:
      return null;
  }
}

interface SidebarProps {
  daemonRunning: boolean;
}

export function Sidebar({ daemonRunning }: SidebarProps) {
  const { data: stats } = useQuery({
    queryKey: ["meeting-stats"],
    queryFn: getMeetingStats,
    enabled: daemonRunning,
    refetchInterval: 30_000,
  });

  const badgeCount = (stats?.pending_count ?? 0) + (stats?.error_count ?? 0);

  return (
    <aside className="flex flex-col w-[220px] bg-sidebar border-r border-border shrink-0 h-full">
      {/* Titlebar drag region */}
      <div data-tauri-drag-region className="h-[52px] flex items-end px-4 pb-2">
        <span className="text-sm font-semibold text-text-primary tracking-tight">
          MeetingMind
        </span>
      </div>

      {/* Navigation */}
      <nav
        aria-label="Main navigation"
        className="flex flex-col gap-0.5 px-2 mt-2 flex-1"
      >
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            aria-label={item.label}
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-3 py-1.5 rounded-md text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-sidebar ${
                isActive
                  ? "bg-sidebar-hover text-text-primary"
                  : "text-text-secondary hover:bg-sidebar-hover hover:text-text-primary"
              }`
            }
          >
            <Icon name={item.icon} />
            {item.label}
            {item.to === "/meetings" && badgeCount > 0 && (
              <span className="ml-auto text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-status-error/20 text-status-error">
                {badgeCount}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

      <button
        onClick={() =>
          window.dispatchEvent(new CustomEvent("toggle-notifications"))
        }
        className="mx-2 mb-2 flex items-center gap-2.5 px-3 py-1.5 rounded-md text-sm text-text-secondary hover:bg-sidebar-hover hover:text-text-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-sidebar"
        aria-label="Notifications"
      >
        <Icon name="bell" />
        Notifications
        <NotificationBadge />
      </button>

      {/* Daemon status */}
      <div
        className="px-4 py-3 border-t border-border"
        role="status"
        aria-live="polite"
      >
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <span
            aria-hidden="true"
            className={`w-2 h-2 rounded-full ${
              daemonRunning ? "bg-status-idle" : "bg-status-error"
            }`}
          />
          {daemonRunning ? "Daemon connected" : "Daemon offline"}
        </div>
      </div>
    </aside>
  );
}
