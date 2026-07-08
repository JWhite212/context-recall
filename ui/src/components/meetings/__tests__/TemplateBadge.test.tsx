import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TemplateBadge } from "../TemplateBadge";

describe("TemplateBadge", () => {
  it("shows the template name", () => {
    render(<TemplateBadge name="discovery" source="auto" />);
    expect(screen.getByText("discovery")).toBeInTheDocument();
  });

  it("shows a manual badge when the source is manual", () => {
    render(<TemplateBadge name="discovery" source="manual" />);
    expect(screen.getByText("manual")).toBeInTheDocument();
  });

  it("renders nothing without a name", () => {
    const { container } = render(<TemplateBadge name={null} source="" />);
    expect(container.firstChild).toBeNull();
  });
});
