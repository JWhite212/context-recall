/* ------------------------------------------------------------------ */
/*  Skeleton loading placeholders                                      */
/* ------------------------------------------------------------------ */

export function Skeleton({
  className = "",
}: {
  className?: string;
}) {
  return (
    <div
      className={`animate-pulse rounded-md bg-border/50 ${className}`}
      aria-hidden="true"
    />
  );
}

export function SkeletonLine({
  width = "w-full",
}: {
  width?: string;
}) {
  return <Skeleton className={`h-3.5 ${width}`} />;
}

export function SkeletonCard() {
  return (
    <div
      className="rounded-xl bg-surface-raised border border-border p-6"
      aria-hidden="true"
    >
      <div className="flex items-center gap-3">
        <Skeleton className="w-3 h-3 rounded-full" />
        <div className="flex-1 flex flex-col gap-2">
          <SkeletonLine width="w-24" />
          <SkeletonLine width="w-48" />
        </div>
      </div>
    </div>
  );
}

export function SkeletonMeetingRow() {
  return (
    <div
      className="flex items-center justify-between py-3 px-4 rounded-xl bg-surface-raised border border-border mb-1"
      aria-hidden="true"
    >
      <div className="flex-1 flex flex-col gap-2 min-w-0">
        <SkeletonLine width="w-48" />
        <div className="flex items-center gap-2">
          <Skeleton className="h-3 w-16" />
          <Skeleton className="h-3 w-10" />
        </div>
      </div>
      <Skeleton className="h-5 w-16 rounded-full ml-4" />
    </div>
  );
}
