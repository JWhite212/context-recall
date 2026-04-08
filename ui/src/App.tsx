import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCallback, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";

import { Sidebar } from "./components/layout/Sidebar";
import { Dashboard } from "./components/dashboard/Dashboard";
import { MeetingList } from "./components/meetings/MeetingList";
import { MeetingDetail } from "./components/meetings/MeetingDetail";
import { Settings } from "./components/settings/Settings";
import { LiveView } from "./components/live/LiveView";
import { CommandPalette } from "./components/common/CommandPalette";
import { useDaemonStatus } from "./hooks/useDaemonStatus";
import { useWebSocket } from "./hooks/useWebSocket";
import { useTraySync } from "./hooks/useTraySync";
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
  useTraySync(state);

  // Load auth token from disk via Tauri on mount.
  useEffect(() => {
    invoke<string>("read_auth_token")
      .then(setAuthToken)
      .catch(() => {
        // Token not available yet — daemon may not have started.
      });
  }, []);

  const onWSEvent = useCallback(
    (event: WSEvent) => {
      handleEvent(event);

      // Invalidate meeting queries on pipeline completion.
      if (event.type === "pipeline.complete") {
        queryClient.invalidateQueries({ queryKey: ["meetings"] });
      }
    },
    [handleEvent],
  );

  useWebSocket(onWSEvent);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar daemonRunning={daemonRunning} />
      <CommandPalette />
      <main className="flex-1 overflow-y-auto">
        {/* Titlebar drag region over the content area */}
        <div data-tauri-drag-region className="h-[52px] shrink-0" />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/live" element={<LiveView />} />
          <Route path="/meetings" element={<MeetingList />} />
          <Route path="/meetings/:id" element={<MeetingDetail />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppShell />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
