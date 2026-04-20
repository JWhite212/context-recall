import {
  createBrowserRouter,
  createRoutesFromElements,
  RouterProvider,
  Route,
  Outlet,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { Sidebar } from "./components/layout/Sidebar";
import { Dashboard } from "./components/dashboard/Dashboard";
import { MeetingList } from "./components/meetings/MeetingList";
import { MeetingDetail } from "./components/meetings/MeetingDetail";
import { Settings } from "./components/settings/Settings";
import { Search } from "./components/search/Search";
import { LiveView } from "./components/live/LiveView";
import { CalendarView } from "./components/calendar/CalendarView";
import { ActionItemList } from "./components/action-items/ActionItemList";
import { CommandPalette } from "./components/common/CommandPalette";
import { ToastProvider } from "./components/common/Toast";
import {
  OnboardingWizard,
  isOnboardingComplete,
} from "./components/onboarding/OnboardingWizard";
import { useDaemonStatus } from "./hooks/useDaemonStatus";
import { useWebSocket } from "./hooks/useWebSocket";
import { useTraySync } from "./hooks/useTraySync";
import { useNotifications } from "./hooks/useNotifications";
import { useAppStore } from "./stores/appStore";
import { setAuthToken } from "./lib/api";
import type { WSEvent } from "./lib/types";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      refetchOnWindowFocus: true,
    },
  },
});

function AppShell() {
  const { daemonRunning, state } = useDaemonStatus();
  const handleEvent = useAppStore((s) => s.handleEvent);
  const [lastEvent, setLastEvent] = useState<WSEvent | null>(null);
  const [showOnboarding, setShowOnboarding] = useState(!isOnboardingComplete());
  useTraySync(state);
  useNotifications(lastEvent);

  const onWSEvent = useCallback(
    (event: WSEvent) => {
      handleEvent(event);
      setLastEvent(event);

      // Invalidate meeting queries on pipeline completion or re-summarise status change.
      if (
        event.type === "pipeline.complete" ||
        event.type === "meeting.resummarise"
      ) {
        queryClient.invalidateQueries({ queryKey: ["meetings"] });
        queryClient.invalidateQueries({ queryKey: ["calendar"] });
        queryClient.invalidateQueries({ queryKey: ["calendar-heatmap"] });
        if (event.meeting_id) {
          queryClient.invalidateQueries({
            queryKey: ["meeting", event.meeting_id],
          });
        }
      }
      // Refresh model list on download progress (throttled via staleTime).
      if (
        event.type === "model.download.progress" &&
        (event.percent === 100 || event.error)
      ) {
        queryClient.invalidateQueries({ queryKey: ["models"] });
      }
    },
    [handleEvent],
  );

  useWebSocket(onWSEvent);

  if (showOnboarding) {
    return <OnboardingWizard onComplete={() => setShowOnboarding(false)} />;
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <a href="#main-content" className="skip-to-content">
        Skip to content
      </a>
      <Sidebar daemonRunning={daemonRunning} />
      <CommandPalette />
      <main id="main-content" className="flex-1 overflow-y-auto" role="main">
        {/* Titlebar drag region over the content area */}
        <div data-tauri-drag-region className="h-[52px] shrink-0" />
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}

const router = createBrowserRouter(
  createRoutesFromElements(
    <Route element={<AppShell />}>
      <Route path="/" element={<Dashboard />} />
      <Route path="/live" element={<LiveView />} />
      <Route path="/meetings" element={<MeetingList />} />
      <Route path="/meetings/:id" element={<MeetingDetail />} />
      <Route path="/calendar" element={<CalendarView />} />
      <Route path="/action-items" element={<ActionItemList />} />
      <Route path="/search" element={<Search />} />
      <Route path="/settings" element={<Settings />} />
    </Route>,
  ),
);

export default function App() {
  const [authReady, setAuthReady] = useState(false);
  const attempted = useRef(false);

  useEffect(() => {
    if (attempted.current) return;
    attempted.current = true;
    invoke<string>("read_auth_token")
      .then(setAuthToken)
      .catch(() => {
        // Token not available yet — daemon may not have started.
      })
      .finally(() => setAuthReady(true));
  }, []);

  if (!authReady) return null;

  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  );
}
