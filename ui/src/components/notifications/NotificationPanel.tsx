import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";

import {
  getNotifications,
  getUnreadCount,
  dismissNotification,
} from "../../lib/api";
import { useAppStore } from "../../stores/appStore";
import type { AppNotification } from "../../lib/types";

export function NotificationPanel() {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const setUnreadNotifications = useAppStore((s) => s.setUnreadNotifications);

  // Listen for toggle-notifications custom event.
  useEffect(() => {
    const handler = () => setOpen((prev) => !prev);
    window.addEventListener("toggle-notifications", handler);
    return () => window.removeEventListener("toggle-notifications", handler);
  }, []);

  // Fetch notifications (only when panel is open).
  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => getNotifications(50),
    enabled: open,
    refetchInterval: open ? 10_000 : false,
  });

  // Poll unread count every 30s.
  useQuery({
    queryKey: ["notifications-unread"],
    queryFn: async () => {
      const res = await getUnreadCount();
      setUnreadNotifications(res.count);
      return res;
    },
    refetchInterval: 30_000,
  });

  // Dismiss mutation.
  const dismiss = useMutation({
    mutationFn: dismissNotification,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
      queryClient.invalidateQueries({ queryKey: ["notifications-unread"] });
    },
  });

  const close = useCallback(() => setOpen(false), []);

  const notifications: AppNotification[] = data?.notifications ?? [];

  return (
    <AnimatePresence>
      {open && (
        <>
          {/* Backdrop */}
          <motion.div
            key="notification-backdrop"
            className="fixed inset-0 bg-black/30 z-40"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={close}
          />

          {/* Panel */}
          <motion.aside
            key="notification-panel"
            className="fixed right-0 top-0 bottom-0 w-[360px] bg-surface-raised border-l border-border z-50 flex flex-col"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", damping: 25, stiffness: 200 }}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-border">
              <h2 className="text-lg font-semibold">Notifications</h2>
              <button
                onClick={close}
                className="p-1 rounded hover:bg-surface text-muted hover:text-foreground transition-colors"
                aria-label="Close notifications"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>

            {/* Scrollable list */}
            <div className="flex-1 overflow-y-auto">
              {notifications.length === 0 ? (
                <p className="text-muted text-sm text-center py-8">
                  No notifications
                </p>
              ) : (
                <ul className="divide-y divide-border">
                  {notifications.map((n) => (
                    <li
                      key={n.id}
                      className={`px-4 py-3 ${
                        n.status === "dismissed"
                          ? "bg-surface/50 opacity-60"
                          : "bg-surface"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium truncate">
                            {n.title}
                          </p>
                          {n.body && (
                            <p className="text-xs text-muted mt-0.5 line-clamp-2">
                              {n.body}
                            </p>
                          )}
                          <p className="text-xs text-muted mt-1">
                            {formatDistanceToNow(
                              new Date(n.created_at * 1000),
                              { addSuffix: true },
                            )}
                          </p>
                        </div>
                        {n.status !== "dismissed" && (
                          <button
                            onClick={() => dismiss.mutate(n.id)}
                            className="shrink-0 p-1 rounded hover:bg-surface-raised text-muted hover:text-foreground transition-colors"
                            aria-label="Dismiss notification"
                          >
                            <svg
                              xmlns="http://www.w3.org/2000/svg"
                              width="16"
                              height="16"
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            >
                              <line x1="18" y1="6" x2="6" y2="18" />
                              <line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
