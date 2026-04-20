import { useAppStore } from "../../stores/appStore";

export function NotificationBadge() {
  const count = useAppStore((s) => s.unreadNotifications);
  if (count === 0) return null;
  return (
    <span className="ml-auto text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-accent/20 text-accent">
      {count > 99 ? "99+" : count}
    </span>
  );
}
