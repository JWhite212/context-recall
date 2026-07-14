import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Search } from "../Search";
import { makeWrapper } from "../../../test/queryWrapper";
import * as api from "../../../lib/api";

vi.mock("../../../lib/api");

beforeEach(() => {
  vi.mocked(api.getMeetingTags).mockResolvedValue([
    "Type/Discovery",
    "ClientX",
  ]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Search", () => {
  it("suggests distinct meeting tags (not the legacy label field)", async () => {
    render(<Search />, { wrapper: makeWrapper() });

    expect(
      await screen.findByText(/Try searching by tag/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Type/Discovery" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "ClientX" })).toBeInTheDocument();
    expect(api.getMeetingTags).toHaveBeenCalled();
  });

  it("clicking a tag chip fills the query box", async () => {
    render(<Search />, { wrapper: makeWrapper() });

    fireEvent.click(await screen.findByRole("button", { name: "ClientX" }));

    expect(
      screen.getByRole("textbox", { name: /search transcripts/i }),
    ).toHaveValue("ClientX");
  });
});
