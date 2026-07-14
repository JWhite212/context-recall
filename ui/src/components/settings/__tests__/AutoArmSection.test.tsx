import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { AutoArmSection } from "../AutoArmSection";
import { makeWrapper } from "../../../test/queryWrapper";


const CONFIG = {
  auto_arm: {
    enabled: false,
    lead_minutes: 2,
    trailing_minutes: 5,
    activity_rms_dbfs: -45,
    activity_sustain_seconds: 3,
    meeting_process_names: ["zoom.us"],
  },
};

describe("AutoArmSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.includes("/api/config") && init?.method === "PUT") {
        return new Response(JSON.stringify(CONFIG), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      // GET /api/config
      return new Response(JSON.stringify(CONFIG), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("renders the toggle off from config", async () => {
    render(<AutoArmSection id="auto_arm" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Auto-record scheduled meetings" }),
      ).toHaveAttribute("aria-checked", "false"),
    );
  });

  it("PUTs auto_arm.enabled=true when toggled on", async () => {
    render(<AutoArmSection id="auto_arm" />, { wrapper: makeWrapper() });
    const sw = await screen.findByRole("switch", {
      name: "Auto-record scheduled meetings",
    });

    fireEvent.click(sw);

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "PUT")!;
    const body = JSON.parse(init?.body as string);
    expect(body.auto_arm.enabled).toBe(true);
  });
});
