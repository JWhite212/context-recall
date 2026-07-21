import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CalendarLinkPicker } from "../CalendarLinkPicker";

const CANDIDATES = [
  { id: "a", label: "Amelia Check-In", subtitle: "11:01 · 23m" },
  { id: "b", label: "Standup", subtitle: "10:03 · 28m" },
];

describe("CalendarLinkPicker", () => {
  it("lists candidates and filters by search", () => {
    render(
      <CalendarLinkPicker
        title="Link to calendar event"
        candidates={CANDIDATES}
        emptyLabel="Nothing nearby"
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText("Amelia Check-In")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "stand" },
    });
    expect(screen.queryByText("Amelia Check-In")).not.toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
  });

  it("calls onPick with the chosen id", () => {
    const onPick = vi.fn();
    render(
      <CalendarLinkPicker
        title="t"
        candidates={CANDIDATES}
        emptyLabel="e"
        onPick={onPick}
        onClose={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("Standup"));
    expect(onPick).toHaveBeenCalledWith("b");
  });

  it("shows the empty label when no candidates", () => {
    render(
      <CalendarLinkPicker
        title="t"
        candidates={[]}
        emptyLabel="Nothing nearby"
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText("Nothing nearby")).toBeInTheDocument();
  });
});
