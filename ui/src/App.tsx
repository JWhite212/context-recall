import {
  createBrowserRouter,
  createRoutesFromElements,
  RouterProvider,
  Route,
  Outlet,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { DaemonConnectionScreen } from "./components/system/DaemonConnectionScreen";
import { useDaemonConnection } from "./hooks/useDaemonConnection";
import { Sidebar } from "./components/layout/Sidebar";
import { Dashboard } from "./components/dashboard/Dashboard";
import { MeetingList } from "./components/meetings/MeetingList";
import { MeetingDetail } from "./components/meetings/MeetingDetail";
import { Settings } from "./components/settings/Settings";
import { Search } from "./components/search/Search";
import { LiveView } from "./components/live/LiveView";
import { CalendarView } from "./components/calendar/CalendarView";
import { InsightsPanel } from "./components/insights/InsightsPanel";
import { ActionItemList } from "./components/action-items/ActionItemList";
import { PrepBriefing } from "./components/prep/PrepBriefing";
import { SeriesList } from "./components/series/SeriesList";
import { SeriesDetail } from "./components/series/SeriesDetail";
import { CommandPalette } from "./components/common/CommandPalette";
import { NotificationPanel } from "./components/notifications/NotificationPanel";
import { ToastProvider } from "./components/common/Toast";
import {
  OnboardingWizard,
  isOnboardingComplete,
} from "./components/onboarding/OnboardingWizard";
import { useDaemonStatus } from "./hooks/useDaemonStatus";
import { useWebSocket } from "./hooks/useWebSocket";
import { useTraySync } from "./hooks/useTraySync";
import { useNotifications } from "./hooks/useNotifications";
import { usePipelineSync } from "./hooks/usePipelineSync";
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
  usePipelineSync();

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
      if (event.type === "action_items.extracted") {
        queryClient.invalidateQueries({ queryKey: ["action-items"] });
      }
      if (event.type === "notification") {
        queryClient.invalidateQueries({ queryKey: ["notifications-unread"] });
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
        <Outlet />
      </main>
      <NotificationPanel />
    </div>
  );
}

// Each top-level route is wrapped in its own ErrorBoundary so a crash on one
// screen doesn't blank the rest of the app.
function boundary(node: React.ReactNode) {
  return <ErrorBoundary>{node}</ErrorBoundary>;
}

const router = createBrowserRouter(
  createRoutesFromElements(
    <Route element={<AppShell />}>
      <Route path="/" element={boundary(<Dashboard />)} />
      <Route path="/live" element={boundary(<LiveView />)} />
      <Route path="/meetings" element={boundary(<MeetingList />)} />
      <Route path="/meetings/:id" element={boundary(<MeetingDetail />)} />
      <Route path="/calendar" element={boundary(<CalendarView />)} />
      <Route path="/action-items" element={boundary(<ActionItemList />)} />
      <Route path="/insights" element={boundary(<InsightsPanel />)} />
      <Route path="/prep" element={boundary(<PrepBriefing />)} />
      <Route path="/prep/:meetingId" element={boundary(<PrepBriefing />)} />
      <Route path="/series" element={boundary(<SeriesList />)} />
      <Route path="/series/:id" element={boundary(<SeriesDetail />)} />
      <Route path="/search" element={boundary(<Search />)} />
      <Route path="/settings" element={boundary(<Settings />)} />
    </Route>,
  ),
);

export default function App() {
  const { state, error, token, retry, startLocal } = useDaemonConnection();

  useEffect(() => {
    if (token) setAuthToken(token);
  }, [token]);

  if (state !== "connected") {
    return (
      <DaemonConnectionScreen
        state={state}
        error={error}
        onRetry={retry}
        onStartLocal={() => {
          void startLocal();
        }}
        onOpenLogs={() => {
          void invoke("open_logs_dir");
        }}
        onOpenAppSupport={() => {
          void invoke("open_app_support_dir");
        }}
      />
    );
  }

  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  );
}
