import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EmptyState } from "../EmptyState";

describe("EmptyState", () => {
  it("renders the title and optional description", () => {
    render(<EmptyState title="Nothing here" description="No meetings yet" />);

    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(screen.getByText("No meetings yet")).toBeInTheDocument();
  });

  it("renders the action when provided", () => {
    render(
      <EmptyState
        title="Empty"
        action={<button type="button">Do something</button>}
      />,
    );

    expect(
      screen.getByRole("button", { name: /do something/i }),
    ).toBeInTheDocument();
  });
});
