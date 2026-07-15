import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ActionItemList, groupItems } from "../ActionItemList";
import { makeWrapper } from "../../../test/queryWrapper";
import type { ActionItem, Client, Project } from "../../../lib/types";

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

describe("ActionItemList", () => {
  let calls: { url: string; method: string }[];

  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        const method = init?.method ?? "GET";
        calls.push({ url, method });
        if (url.includes("/api/action-items")) {
          return new Response(
            JSON.stringify({
              items: [
                makeItem({ id: "i1", title: "Open item", status: "open" }),
                makeItem({ id: "i2", title: "Done item", status: "done" }),
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
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

  it("selecting a project filter refetches with project_id", async () => {
    render(<ActionItemList />, { wrapper: makeWrapper() });

    await waitFor(() =>
      expect(screen.getByText("Open item")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Filter by project"), {
      target: { value: "p1" },
    });

    await waitFor(() => {
      const refetch = calls.find(
        (c) =>
          c.url.includes("/api/action-items") &&
          c.url.includes("project_id=p1"),
      );
      expect(refetch).toBeTruthy();
    });
  });

  it("changing the client filter resets the project filter", async () => {
    render(<ActionItemList />, { wrapper: makeWrapper() });

    await waitFor(() =>
      expect(screen.getByText("Open item")).toBeInTheDocument(),
    );

    const projectSelect = screen.getByLabelText(
      "Filter by project",
    ) as HTMLSelectElement;
    fireEvent.change(projectSelect, { target: { value: "p1" } });

    await waitFor(() =>
      expect(
        calls.find(
          (c) =>
            c.url.includes("/api/action-items") &&
            c.url.includes("project_id=p1"),
        ),
      ).toBeTruthy(),
    );

    fireEvent.change(screen.getByLabelText("Filter by client"), {
      target: { value: "c1" },
    });

    // The stale project must not linger in the select or the query.
    expect(projectSelect.value).toBe("");
    await waitFor(() => {
      const refetch = calls.find(
        (c) =>
          c.url.includes("/api/action-items") && c.url.includes("client_id=c1"),
      );
      expect(refetch).toBeTruthy();
      expect(refetch!.url).not.toContain("project_id");
    });
  });

  it("renders items under status group headers when grouped by status", async () => {
    render(<ActionItemList />, { wrapper: makeWrapper() });

    await waitFor(() =>
      expect(screen.getByText("Open item")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Group by"), {
      target: { value: "status" },
    });

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Open" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Done" })).toBeInTheDocument();
    });
  });
});

describe("groupItems", () => {
  const clients: Client[] = [
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
  ];
  const projects: Project[] = [
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
  ];

  it("returns a single unlabelled group for 'none'", () => {
    const items = [makeItem({ id: "i1" }), makeItem({ id: "i2" })];
    const groups = groupItems(items, "none", { clients, projects });
    expect(groups).toHaveLength(1);
    expect(groups[0].items).toHaveLength(2);
  });

  it("groups by client, resolving id to name and falling back to Unassigned", () => {
    const items = [
      makeItem({ id: "i1", client_id: "c1" }),
      makeItem({ id: "i2", client_id: null }),
    ];
    const groups = groupItems(items, "client", { clients, projects });
    const labels = groups.map((g) => g.label).sort();
    expect(labels).toEqual(["Acme", "Unassigned"]);
  });

  it("groups by project, resolving id to name and falling back to Unassigned", () => {
    const items = [
      makeItem({ id: "i1", project_id: "p1" }),
      makeItem({ id: "i2", project_id: null }),
    ];
    const groups = groupItems(items, "project", { clients, projects });
    const labels = groups.map((g) => g.label).sort();
    expect(labels).toEqual(["Unassigned", "Website Revamp"]);
  });

  it("groups by meeting_id", () => {
    const items = [
      makeItem({ id: "i1", meeting_id: "m1" }),
      makeItem({ id: "i2", meeting_id: "m2" }),
    ];
    const groups = groupItems(items, "meeting", { clients, projects });
    expect(groups.map((g) => g.key).sort()).toEqual(["m1", "m2"]);
  });

  it("buckets by due date into overdue/today/this-week/later/no-date", () => {
    const now = new Date();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const nextMonth = new Date(now);
    nextMonth.setDate(now.getDate() + 40);

    const items = [
      makeItem({ id: "i1", due_date: yesterday.toISOString() }),
      makeItem({ id: "i2", due_date: null }),
      makeItem({ id: "i3", due_date: nextMonth.toISOString() }),
    ];
    const groups = groupItems(items, "due", { clients, projects });
    const keys = groups.map((g) => g.key);
    expect(keys).toContain("overdue");
    expect(keys).toContain("no-date");
    expect(keys).toContain("later");
  });
});
