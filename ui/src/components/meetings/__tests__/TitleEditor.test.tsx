import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { TitleEditor } from "../TitleEditor";
import { makeWrapper } from "../../../test/queryWrapper";

describe("TitleEditor", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            meeting_id: "m1",
            title: "Renamed",
            title_source: "manual",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  it("commits a new title on Enter", async () => {
    const onRenamed = vi.fn();
    render(<TitleEditor meetingId="m1" title="Old" onRenamed={onRenamed} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByText("Old"));
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith("Renamed"));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/meetings/m1"),
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it("does not fire a second PATCH when blur follows Enter (M2)", async () => {
    // Enter commits and disables the input; the browser then blurs the
    // disabled input, which used to re-enter commit() and PATCH twice.
    const onRenamed = vi.fn();
    render(<TitleEditor meetingId="m1" title="Old" onRenamed={onRenamed} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByText("Old"));
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);
    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith("Renamed"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("cancels on Escape without calling the API", () => {
    render(<TitleEditor meetingId="m1" title="Old" />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByText("Old"));
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Nope" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByText("Old")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not call the API for an unchanged title", () => {
    render(<TitleEditor meetingId="m1" title="Old" />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByText("Old"));
    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
