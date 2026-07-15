import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ActionItemCard } from "../ActionItemCard";
import { makeWrapper } from "../../../test/queryWrapper";
import type { ActionItem } from "../../../lib/types";

function makeItem(overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    id: "i1",
    meeting_id: "m1",
    title: "Do the thing",
    description: null,
    assignee: null,
    status: "open",
    priority: "medium",
    due_date: null,
    reminder_at: null,
    source: "manual",
    extracted_text: null,
    client_id: null,
    project_id: null,
    tag_source: undefined,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

describe("ActionItemCard", () => {
  let calls: { url: string; method: string; body: unknown }[];

  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(init.body as string) : undefined;
        calls.push({ url, method, body });
        if (url.includes("/api/clients")) {
          return new Response(
            JSON.stringify([
              {
                id: "c1",
                name: "Acme",
                description: "",
                aliases: [],
                email_domains: [],
                status: "active",
                created_at: 1,
                updated_at: 1,
              },
            ]),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.includes("/api/projects")) {
          return new Response(
            JSON.stringify([
              {
                id: "p1",
                client_id: "c1",
                name: "Website Revamp",
                description: "",
                aliases: [],
                status: "active",
                created_at: 1,
                updated_at: 1,
              },
            ]),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;
  });

  it("shows the resolved client/project name when the item is tagged", async () => {
    render(
      <ActionItemCard item={makeItem({ client_id: "c1", project_id: "p1" })} />,
      {
        wrapper: makeWrapper(),
      },
    );

    await waitFor(() => {
      expect(screen.getByText("Acme")).toBeInTheDocument();
      expect(screen.getByText("Website Revamp")).toBeInTheDocument();
    });
  });

  it("opens the tag editor and PATCHes client_id/project_id on change", async () => {
    render(<ActionItemCard item={makeItem()} />, { wrapper: makeWrapper() });

    fireEvent.click(await screen.findByLabelText("Edit client/project tag"));

    await waitFor(() =>
      expect(screen.getByLabelText("Tag client")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Tag client"), {
      target: { value: "c1" },
    });

    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH");
      expect(patch?.url).toContain("/api/action-items/i1");
      expect(patch?.body).toMatchObject({ client_id: "c1" });
    });

    fireEvent.change(screen.getByLabelText("Tag project"), {
      target: { value: "p1" },
    });

    await waitFor(() => {
      const patch = calls.find(
        (c) =>
          c.method === "PATCH" &&
          (c.body as { project_id?: string })?.project_id,
      );
      expect(patch?.url).toContain("/api/action-items/i1");
      expect(patch?.body).toMatchObject({ project_id: "p1" });
    });
  });
});
