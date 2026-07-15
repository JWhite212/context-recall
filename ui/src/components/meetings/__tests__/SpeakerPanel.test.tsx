import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SpeakerPanel } from "../SpeakerPanel";
import { makeWrapper } from "../../../test/queryWrapper";

const segments = [
  { start: 0, end: 1, text: "hi", speaker: "SPEAKER_00" },
  { start: 1, end: 2, text: "hello", speaker: "SPEAKER_01" },
  { start: 2, end: 3, text: "bye", speaker: "SPEAKER_00" },
] as never[];

describe("SpeakerPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(
      async () =>
        new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  it("lists each detected speaker with its segment count", () => {
    render(
      <SpeakerPanel meetingId="m1" segments={segments} onSeek={vi.fn()} />,
      {
        wrapper: makeWrapper(),
      },
    );
    expect(screen.getByText("SPEAKER_00")).toBeInTheDocument();
    expect(screen.getByText("SPEAKER_01")).toBeInTheDocument();
    expect(screen.getByText(/2 segments/i)).toBeInTheDocument(); // SPEAKER_00
  });

  it("seeks to a speaker's first segment on Play", () => {
    const onSeek = vi.fn();
    render(
      <SpeakerPanel meetingId="m1" segments={segments} onSeek={onSeek} />,
      {
        wrapper: makeWrapper(),
      },
    );
    fireEvent.click(
      screen.getAllByRole("button", { name: /play .* segments/i })[1],
    ); // SPEAKER_01
    expect(onSeek).toHaveBeenCalledWith(1);
  });

  it("renames a speaker via the API", async () => {
    render(
      <SpeakerPanel meetingId="m1" segments={segments} onSeek={vi.fn()} />,
      {
        wrapper: makeWrapper(),
      },
    );
    fireEvent.click(screen.getAllByRole("button", { name: /rename/i })[0]);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Alice" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/meetings/m1/speakers/SPEAKER_00"),
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
  });
});
