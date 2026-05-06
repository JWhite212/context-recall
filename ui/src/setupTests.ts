import "@testing-library/jest-dom";
import { vi } from "vitest";

// Mock the Tauri core module so any code that calls `invoke(...)` in
// a unit test does not crash with "window.__TAURI_INTERNALS__ is
// undefined". Individual tests can override the implementation via
// `vi.mocked(invoke).mockImplementation(...)`.
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string) => {
    switch (cmd) {
      case "read_auth_token":
        return "test-token";
      case "start_daemon":
        return null;
      case "open_logs_dir":
      case "open_app_support_dir":
        return null;
      default:
        return null;
    }
  }),
}));

// Provide a default fetch that returns a 200 OK with an empty JSON
// body. Tests that need specific responses can replace this stub.
if (!globalThis.fetch) {
  globalThis.fetch = vi.fn(
    async () =>
      new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  ) as unknown as typeof fetch;
}
