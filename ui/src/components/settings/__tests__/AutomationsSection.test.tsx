import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { AutomationsSection } from "../AutomationsSection";
import { makeWrapper } from "../../../test/queryWrapper";

describe("AutomationsSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.includes("/api/automation-rules") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            id: "r-new",
            name: "New rule",
            enabled: true,
            match_mode: "all",
            conditions: [],
            actions: [],
            created_at: 2,
            updated_at: 2,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/automation-rules")) {
        return new Response(
          JSON.stringify([
            {
              id: "r1",
              name: "Tag discovery",
              enabled: true,
              match_mode: "all",
              conditions: [{ field: "tag", value: "Type/Discovery" }],
              actions: [{ type: "apply_tag", tags: ["Reviewed"] }],
              created_at: 1,
              updated_at: 1,
            },
            {
              id: "r2",
              name: "Post-meeting summary",
              enabled: true,
              match_mode: "all",
              conditions: [{ field: "tag", value: "Type/Standup" }],
              actions: [
                { type: "run_insight", definition_id: "d1" },
                { type: "run_insight", definition_id: "unknown-id" },
                { type: "send_notes", url: "https://example.com/hook" },
              ],
              created_at: 2,
              updated_at: 2,
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/insight-definitions")) {
        return new Response(
          JSON.stringify([
            {
              id: "d1",
              name: "Client Call Details",
              prompt: "Summarise client details",
              enabled: true,
              output_mode: "list",
              fields: null,
              created_at: 1,
              updated_at: 1,
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("[]", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  const findPostCall = () => {
    const call = fetchMock.mock.calls.find((args) => {
      const init = args[1] as RequestInit | undefined;
      return (
        (args[0] as RequestInfo | URL)
          .toString()
          .includes("/api/automation-rules") && init?.method === "POST"
      );
    });
    if (!call) throw new Error("no POST /api/automation-rules call found");
    return JSON.parse((call[1] as RequestInit).body as string);
  };

  it("lists existing automation rules", async () => {
    render(<AutomationsSection id="automations" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(screen.getByText("Tag discovery")).toBeInTheDocument(),
    );
  });

  it("labels run_insight (resolved + fallback) and send_notes actions on existing rules", async () => {
    render(<AutomationsSection id="automations" />, { wrapper: makeWrapper() });

    await waitFor(() =>
      expect(
        screen.getByText(/run insight: Client Call Details/),
      ).toBeInTheDocument(),
    );
    // Unresolved definition id falls back to displaying the raw id.
    expect(screen.getByText(/run insight: unknown-id/)).toBeInTheDocument();
    expect(
      screen.getByText(/send notes → https:\/\/example\.com\/hook/),
    ).toBeInTheDocument();
  });

  it("configures a run_insight action referencing a definition and submits it", async () => {
    render(<AutomationsSection id="automations" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(screen.getByText("Tag discovery")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Rule name"), {
      target: { value: "Send client insight" },
    });
    fireEvent.change(screen.getByLabelText("Condition value"), {
      target: { value: "Acme Corp" },
    });
    fireEvent.change(screen.getByLabelText("Action type"), {
      target: { value: "run_insight" },
    });

    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "Client Call Details" }),
      ).toBeInTheDocument(),
    );

    // Runtime-guarded: no definition selected yet -> submit stays disabled.
    expect(screen.getByRole("button", { name: /add rule/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Insight"), {
      target: { value: "d1" },
    });

    const submit = screen.getByRole("button", { name: /add rule/i });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(findPostCall().actions).toEqual([
        expect.objectContaining({ type: "run_insight", definition_id: "d1" }),
      ]);
    });
  });

  it("configures a send_notes action with url, secret, and include_transcript", async () => {
    render(<AutomationsSection id="automations" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(screen.getByText("Tag discovery")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Rule name"), {
      target: { value: "Notify on wrap-up" },
    });
    fireEvent.change(screen.getByLabelText("Condition value"), {
      target: { value: "Acme Corp" },
    });
    fireEvent.change(screen.getByLabelText("Action type"), {
      target: { value: "send_notes" },
    });

    // Runtime-guarded: no url yet -> submit stays disabled.
    expect(screen.getByRole("button", { name: /add rule/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Webhook URL"), {
      target: { value: "https://example.com/hook" },
    });
    fireEvent.change(screen.getByLabelText("Webhook secret"), {
      target: { value: "shh" },
    });
    fireEvent.click(screen.getByLabelText("Include transcript"));

    const submit = screen.getByRole("button", { name: /add rule/i });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(findPostCall().actions).toEqual([
        expect.objectContaining({
          type: "send_notes",
          url: "https://example.com/hook",
          secret: "shh",
          include_transcript: true,
        }),
      ]);
    });
  });
});
