import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { InsightResults } from "../MeetingInsights";
import type {
  InsightDefinition,
  MeetingInsightResult,
} from "../../../lib/types";

const results: MeetingInsightResult[] = [
  {
    definition_id: "d1",
    definition_name: "Risks",
    content: "a",
    speaker: "",
    fields: null,
  },
  {
    definition_id: "d1",
    definition_name: "Risks",
    content: "b",
    speaker: "Me",
    fields: null,
  },
  {
    definition_id: "d2",
    definition_name: "Decisions",
    content: "c",
    speaker: "",
    fields: null,
  },
];

const structuredDefinitions: InsightDefinition[] = [
  {
    id: "d3",
    name: "Client Call Details",
    prompt: "Extract the client call details.",
    enabled: true,
    output_mode: "structured",
    fields: [
      { key: "go_live_date", label: "Go-live date", type: "date" },
      { key: "blockers", label: "Blockers", type: "list" },
    ],
    created_at: 0,
    updated_at: 0,
  },
];

const structuredResults: MeetingInsightResult[] = [
  {
    definition_id: "d3",
    definition_name: "Client Call Details",
    content: "Go-live: 2026-09-02",
    speaker: "",
    fields: {
      go_live_date: "2026-09-02",
      blockers: ["A", "B"],
      owner_next_step: null,
    },
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

  it("renders a structured insight as a labelled card, joining arrays and humanising unmapped keys", () => {
    render(
      <InsightResults
        results={structuredResults}
        definitions={structuredDefinitions}
      />,
    );
    // definition title still shown, as for list mode
    expect(screen.getByText("Client Call Details")).toBeInTheDocument();
    // labels come from the definition's field map, not the raw key
    expect(screen.getByText("Go-live date")).toBeInTheDocument();
    expect(screen.getByText("2026-09-02")).toBeInTheDocument();
    expect(screen.getByText("Blockers")).toBeInTheDocument();
    // arrays join with "; "
    expect(screen.getByText("A; B")).toBeInTheDocument();
    // a field key absent from the definition's field map falls back to a humanised key
    expect(screen.getByText("owner next step")).toBeInTheDocument();
    // null renders as an em dash
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("still renders a structured insight without a definitions map, humanising every key", () => {
    render(<InsightResults results={structuredResults} />);
    expect(screen.getByText("go live date")).toBeInTheDocument();
    expect(screen.getByText("blockers")).toBeInTheDocument();
    expect(screen.getByText("A; B")).toBeInTheDocument();
  });
});
