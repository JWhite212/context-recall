import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AutomationBadges } from "../MeetingInsights";

describe("AutomationBadges", () => {
  it("renders a pill per fired rule", () => {
    render(<AutomationBadges names={["Tag discovery", "Notify me"]} />);
    expect(screen.getByText("Tag discovery")).toBeInTheDocument();
    expect(screen.getByText("Notify me")).toBeInTheDocument();
  });

  it("renders nothing when empty", () => {
    const { container } = render(<AutomationBadges names={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
