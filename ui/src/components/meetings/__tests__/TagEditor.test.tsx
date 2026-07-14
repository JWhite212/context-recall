import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { TagEditor } from "../TagEditor";
import { makeWrapper } from "../../../test/queryWrapper";


describe("TagEditor", () => {
  let calls: { url: string; method: string; body: unknown }[];

  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(init.body as string) : undefined;
        calls.push({ url, method, body });
        if (url.includes("/api/meetings/tags")) {
          return new Response(JSON.stringify({ tags: ["existing"] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;
  });

  it("adds a tag via Enter and PATCHes the full array", async () => {
    render(<TagEditor meetingId="m1" tags={["budget"]} />, {
      wrapper: makeWrapper(),
    });
    const input = screen.getByLabelText("Add meeting tag");
    fireEvent.change(input, { target: { value: "planning" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH");
      expect(patch?.url).toContain("/api/meetings/m1/tags");
      expect(patch?.body).toEqual({ tags: ["budget", "planning"] });
    });
  });

  it("removes a tag and PATCHes the remaining array", async () => {
    render(<TagEditor meetingId="m1" tags={["budget", "planning"]} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByLabelText("Remove tag budget"));
    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH");
      expect(patch?.body).toEqual({ tags: ["planning"] });
    });
  });

  it("blocks edits while a save is in flight", async () => {
    let releasePatch: (r: Response) => void = () => {};
    globalThis.fetch = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PATCH") {
          return new Promise<Response>((resolve) => {
            releasePatch = resolve;
          });
        }
        return Promise.resolve(
          new Response(JSON.stringify({ tags: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      },
    ) as unknown as typeof fetch;

    render(<TagEditor meetingId="m1" tags={["budget"]} />, {
      wrapper: makeWrapper(),
    });
    // Start a removal — the PATCH hangs, keeping the mutation pending.
    fireEvent.click(screen.getByLabelText("Remove tag budget"));

    await waitFor(() =>
      expect(screen.getByLabelText("Remove tag budget")).toBeDisabled(),
    );

    releasePatch(
      new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  });
});
