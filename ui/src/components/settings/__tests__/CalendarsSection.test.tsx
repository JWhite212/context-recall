import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { CalendarsSection } from "../CalendarsSection";
import { makeWrapper } from "../../../test/queryWrapper";

vi.mock("@tauri-apps/plugin-opener", () => ({
  openUrl: vi.fn(async () => {}),
}));

describe("CalendarsSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let permissionStatus = "authorized";
  let permissionGranted = true;
  let calendarsList: { id: string; title: string }[] = [];

  beforeEach(() => {
    permissionStatus = "authorized";
    permissionGranted = true;
    calendarsList = [
      { id: "c1", title: "Work" },
      { id: "c2", title: "Personal" },
    ];
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
            calendars: calendarsList,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/calendar/permission")) {
        return new Response(
          JSON.stringify({
            status: permissionStatus,
            granted: permissionGranted,
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
    vi.mocked(openUrl).mockClear();
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

  it("shows a permission banner when calendar access is not granted", async () => {
    permissionStatus = "denied";
    permissionGranted = false;
    render(<CalendarsSection />, { wrapper: makeWrapper() });
    expect(
      await screen.findByText(/calendar access is not granted/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /open system settings/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sync now/i })).toBeDisabled();
  });

  it("clicking Open System Settings opens the Calendars pane via the opener plugin", async () => {
    permissionStatus = "denied";
    permissionGranted = false;
    render(<CalendarsSection />, { wrapper: makeWrapper() });

    const btn = await screen.findByRole("button", {
      name: /open system settings/i,
    });
    fireEvent.click(btn);

    // Must go through the Tauri opener plugin — a plain window.open() of a
    // custom x-apple.systempreferences: scheme is silently dropped by the
    // WKWebview and never reaches macOS.
    await waitFor(() =>
      expect(openUrl).toHaveBeenCalledWith(
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
      ),
    );
  });

  it("does not show the permission banner when access is granted", async () => {
    render(<CalendarsSection />, { wrapper: makeWrapper() });
    await screen.findByText("Work");
    expect(
      screen.queryByText(/calendar access is not granted/i),
    ).not.toBeInTheDocument();
  });

  it("shows 'No calendars available.' when access is granted but there are no calendars", async () => {
    permissionStatus = "authorized";
    permissionGranted = true;
    calendarsList = [];
    render(<CalendarsSection />, { wrapper: makeWrapper() });

    expect(
      await screen.findByText(/no calendars available/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/calendar access is not granted/i),
    ).not.toBeInTheDocument();
  });
});
