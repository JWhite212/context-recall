import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { InsightsSection } from "../InsightsSection";
import { makeWrapper } from "../../../test/queryWrapper";

describe("InsightsSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.includes("/api/insight-definitions") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            id: "d2",
            name: "Go-live tracker",
            prompt: "Track the go-live date",
            enabled: true,
            output_mode: "structured",
            fields: [
              { key: "go_live_date", label: "Go-live date", type: "date" },
            ],
            created_at: 2,
            updated_at: 2,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/insight-definitions")) {
        return new Response(
          JSON.stringify([
            {
              id: "d1",
              name: "Risks",
              prompt: "List risks",
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

  it("lists existing insight definitions", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());
  });

  it("lets the user add a typed field in structured mode and submits it", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Insight name"), {
      target: { value: "Go-live tracker" },
    });
    fireEvent.change(screen.getByLabelText("Insight prompt"), {
      target: { value: "Track the go-live date" },
    });

    fireEvent.click(screen.getByLabelText(/structured/i));
    fireEvent.click(screen.getByRole("button", { name: /add field/i }));

    fireEvent.change(screen.getByPlaceholderText(/field label/i), {
      target: { value: "Go-live date" },
    });
    fireEvent.change(screen.getByLabelText(/field type/i), {
      target: { value: "date" },
    });

    fireEvent.click(screen.getByRole("button", { name: /add insight/i }));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "POST",
      );
      expect(postCall).toBeTruthy();
    });

    const [url, init] = fetchMock.mock.calls.find(
      ([, i]) => i?.method === "POST",
    )!;
    expect(url.toString()).toContain("/api/insight-definitions");
    const body = JSON.parse(init?.body as string);
    expect(body).toEqual(
      expect.objectContaining({
        name: "Go-live tracker",
        prompt: "Track the go-live date",
        output_mode: "structured",
        fields: [
          expect.objectContaining({
            key: "go_live_date",
            label: "Go-live date",
            type: "date",
          }),
        ],
      }),
    );
  });

  it("disables submit in structured mode with no field rows", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Insight name"), {
      target: { value: "Go-live tracker" },
    });
    fireEvent.change(screen.getByLabelText("Insight prompt"), {
      target: { value: "Track the go-live date" },
    });
    fireEvent.click(screen.getByLabelText(/structured/i));

    expect(screen.getByRole("button", { name: /add insight/i })).toBeDisabled();
  });

  it("disables submit in structured mode with only a blank-label field row", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Insight name"), {
      target: { value: "Go-live tracker" },
    });
    fireEvent.change(screen.getByLabelText("Insight prompt"), {
      target: { value: "Track the go-live date" },
    });
    fireEvent.click(screen.getByLabelText(/structured/i));
    fireEvent.click(screen.getByRole("button", { name: /add field/i }));

    expect(screen.getByRole("button", { name: /add insight/i })).toBeDisabled();
  });

  it("enables submit with one valid field and excludes blank rows from the payload", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Insight name"), {
      target: { value: "Go-live tracker" },
    });
    fireEvent.change(screen.getByLabelText("Insight prompt"), {
      target: { value: "Track the go-live date" },
    });
    fireEvent.click(screen.getByLabelText(/structured/i));

    // A blank-label row that should be excluded from the submitted payload.
    fireEvent.click(screen.getByRole("button", { name: /add field/i }));

    fireEvent.click(screen.getByRole("button", { name: /add field/i }));
    const labelInputs = screen.getAllByPlaceholderText(/field label/i);
    fireEvent.change(labelInputs[1], {
      target: { value: "Go-live date" },
    });
    const typeSelects = screen.getAllByLabelText(/field type/i);
    fireEvent.change(typeSelects[1], {
      target: { value: "date" },
    });

    const submitButton = screen.getByRole("button", { name: /add insight/i });
    expect(submitButton).not.toBeDisabled();
    fireEvent.click(submitButton);

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "POST",
      );
      expect(postCall).toBeTruthy();
    });

    const [, init] = fetchMock.mock.calls.find(
      ([, i]) => i?.method === "POST",
    )!;
    const body = JSON.parse(init?.body as string);
    expect(body.fields).toEqual([
      expect.objectContaining({
        key: "go_live_date",
        label: "Go-live date",
        type: "date",
      }),
    ]);
  });

  it("disables submit when two field rows share the same label (duplicate key)", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Insight name"), {
      target: { value: "Go-live tracker" },
    });
    fireEvent.change(screen.getByLabelText("Insight prompt"), {
      target: { value: "Track the go-live date" },
    });
    fireEvent.click(screen.getByLabelText(/structured/i));

    fireEvent.click(screen.getByRole("button", { name: /add field/i }));
    fireEvent.click(screen.getByRole("button", { name: /add field/i }));
    const labelInputs = screen.getAllByPlaceholderText(/field label/i);
    fireEvent.change(labelInputs[0], { target: { value: "Go-live date" } });
    fireEvent.change(labelInputs[1], { target: { value: "Go-live date" } });

    expect(screen.getByRole("button", { name: /add insight/i })).toBeDisabled();
  });
});
