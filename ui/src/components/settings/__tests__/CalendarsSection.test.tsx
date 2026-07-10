import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CalendarsSection } from "../CalendarsSection";
import { ToastProvider } from "../../common/Toast";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
}

describe("CalendarsSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.includes("/api/calendar/sync")) {
        return new Response(JSON.stringify({ synced: 3 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/api/calendar/calendars")) {
        return new Response(
          JSON.stringify({
            calendars: [
              { id: "c1", title: "Work" },
              { id: "c2", title: "Personal" },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/config")) {
        if (init?.method === "PUT") {
          return new Response(init.body as string, {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(
          JSON.stringify({ calendar: { excluded_calendars: ["Personal"] } }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("lists available calendars", async () => {
    render(<CalendarsSection id="calendars" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Work")).toBeInTheDocument());
    expect(screen.getByText("Personal")).toBeInTheDocument();
  });

  it("unchecking an included calendar PUTs the updated excluded list", async () => {
    render(<CalendarsSection id="calendars" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Work")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("checkbox", { name: "Work" }));

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "PUT" && init?.body,
      );
      expect(putCall).toBeTruthy();
    });

    const [url, init] = fetchMock.mock.calls.find(
      ([, i]) => i?.method === "PUT",
    )!;
    expect(url.toString()).toContain("/api/config");
    expect(init?.method).toBe("PUT");
    const body = JSON.parse(init?.body as string);
    expect(body.calendar.excluded_calendars).toEqual(["Personal", "Work"]);
  });

  it("re-checking an excluded calendar removes it from the PUT body", async () => {
    render(<CalendarsSection id="calendars" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(screen.getByText("Personal")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("checkbox", { name: "Personal" }));

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "PUT" && init?.body,
      );
      expect(putCall).toBeTruthy();
    });

    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "PUT")!;
    const body = JSON.parse(init?.body as string);
    expect(body.calendar.excluded_calendars).toEqual([]);
  });

  it("clicking Sync now POSTs to /api/calendar/sync", async () => {
    render(<CalendarsSection id="calendars" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Work")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));

    await waitFor(() => {
      const syncCall = fetchMock.mock.calls.find(([url]) =>
        url.toString().includes("/api/calendar/sync"),
      );
      expect(syncCall).toBeTruthy();
    });

    const [, init] = fetchMock.mock.calls.find(([url]) =>
      url.toString().includes("/api/calendar/sync"),
    )!;
    expect(init?.method).toBe("POST");
  });
});
