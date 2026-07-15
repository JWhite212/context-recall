import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ToastProvider } from "../../common/Toast";
import { makeTestQueryClient } from "../../../test/queryWrapper";
import { MeetingDetail } from "../MeetingDetail";
import { getMeeting } from "../../../lib/api";
import type { Meeting } from "../../../lib/types";

// Isolate MeetingDetail's own render/hook ordering from its data-fetching
// children (each of which fires its own query and is irrelevant here).
vi.mock("../MeetingInsights", () => ({ MeetingInsights: () => null }));
vi.mock("../SpeakerPanel", () => ({ SpeakerPanel: () => null }));
vi.mock("../TagEditor", () => ({ TagEditor: () => null }));
vi.mock("../TemplateBadge", () => ({ TemplateBadge: () => null }));
vi.mock("../AudioPlayer", () => ({ AudioPlayer: () => null }));
vi.mock("../TitleEditor", () => ({
  TitleEditor: ({ title }: { title: string }) => <span>{title}</span>,
}));
vi.mock("../../clients/AssignmentSelect", () => ({
  AssignmentSelect: () => null,
}));
vi.mock("../../people/AssignSpeakerMenu", () => ({
  AssignSpeakerMenu: () => null,
}));
vi.mock("../../action-items/ActionItemCard", () => ({
  ActionItemCard: () => null,
}));

vi.mock("../../../lib/api", () => ({
  getMeeting: vi.fn(),
  getTemplates: vi.fn(async () => []),
  getMeetingActionItems: vi.fn(async () => ({ items: [] })),
  deleteMeeting: vi.fn(),
  exportMeeting: vi.fn(),
  resummariseMeeting: vi.fn(),
  setSpeakerName: vi.fn(),
  reprocessMeeting: vi.fn(),
}));

function makeMeeting(): Meeting {
  return {
    id: "m1",
    title: "Test Meeting",
    status: "complete",
    started_at: 1_700_000_000,
    duration_seconds: 60,
    language: "en",
    word_count: 100,
    transcript_json: JSON.stringify({
      segments: [{ start: 0, end: 1, text: "hello", speaker: "Me" }],
    }),
    summary_markdown: null,
    audio_path: null,
    tags: [],
    attendees_json: "[]",
    calendar_confidence: 0,
  } as unknown as Meeting;
}

function renderDetail() {
  const client = makeTestQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/meetings/m1"]}>
          <Routes>
            <Route path="/meetings/:id" element={<MeetingDetail />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("MeetingDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders without a hooks error when the meeting resolves after the loading state", async () => {
    // Regression for React error #310 ("rendered more hooks than during the
    // previous render"): a hook must not sit after the isLoading/isError/
    // !meeting early returns. The first render is the loading state (fewer
    // hooks); once getMeeting resolves the component re-renders with the
    // meeting (more hooks) — the hook order must stay stable across both.
    vi.mocked(getMeeting).mockResolvedValue(makeMeeting());

    renderDetail();

    // The loaded render must succeed (this throws #310 if a hook runs after
    // the early returns).
    expect(await screen.findByText("Test Meeting")).toBeInTheDocument();
  });
});
