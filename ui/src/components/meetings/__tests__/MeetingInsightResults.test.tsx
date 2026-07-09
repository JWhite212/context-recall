import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { InsightResults } from "../MeetingInsights";
import type { MeetingInsightResult } from "../../../lib/types";

const results: MeetingInsightResult[] = [
  { definition_id: "d1", definition_name: "Risks", content: "a", speaker: "" },
  {
    definition_id: "d1",
    definition_name: "Risks",
    content: "b",
    speaker: "Me",
  },
  {
    definition_id: "d2",
    definition_name: "Decisions",
    content: "c",
    speaker: "",
  },
];

describe("InsightResults", () => {
  it("groups results by definition name and shows every item", () => {
    render(<InsightResults results={results} />);
    expect(screen.getByText("Risks")).toBeInTheDocument();
    expect(screen.getByText("Decisions")).toBeInTheDocument();
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.getByText("c")).toBeInTheDocument();
  });

  it("renders nothing when there are no results", () => {
    const { container } = render(<InsightResults results={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
