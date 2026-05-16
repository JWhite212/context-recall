/**
 * Toast helpers for surfacing API errors consistently.
 *
 * Centralises the formatting of `ApiError` (timeouts, 401 after retry,
 * server-supplied details) so every screen renders the same message for the
 * same underlying failure.
 */

import { ApiError } from "./api";

/** Shape exposed by `useToast()` — duplicated here to avoid a circular import. */
interface ToastSink {
  error: (message: string) => void;
}

/** Format an unknown thrown value as a single-line user-facing message. */
export function formatApiError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) {
      // Network failure or timeout.
      return err.detail || "Request failed";
    }
    if (err.status === 401) {
      return err.retried
        ? "Authentication failed — please restart the app or check your auth token."
        : "Authentication required.";
    }
    return `${err.detail} (status ${err.status})`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * Render an unknown thrown value as an error toast. Idempotent and safe to
 * call from any handler — formats `ApiError` instances with extra context
 * (timeouts, repeated 401s) and falls back to `err.message` otherwise.
 */
export function toastApiError(toast: ToastSink, err: unknown): void {
  toast.error(formatApiError(err));
}
