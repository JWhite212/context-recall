# Meeting Intelligence Features — Design Spec

**Date:** 2026-04-20
**Branch:** feat/calendar-view-and-teams-association (continuing)
**Approach:** Hybrid — Event Hooks + Lightweight Scheduler (Approach C)

## Overview

Five integrated features that transform MeetingMind from a capture/transcription tool into an intelligent meeting assistant:

1. **Recurring Meeting Detection & Trend Analysis** — identify and group related meetings, surface patterns
2. **Meeting Insights Panel** — analytics dashboard with comparative metrics and health indicators
3. **Action Item Extraction & Tracking** — full task management lifecycle extracted from transcripts
4. **Meeting Preparation Mode** — automatic pre-meeting briefings with context from past meetings
5. **Smart Notifications** — multi-channel alerting for reminders, briefings, and health alerts

All features are additive and non-breaking. They integrate with each other through shared data and a common notification dispatcher, but each degrades gracefully if dependencies are unavailable.

---

## Architecture

### Approach: Hybrid (Event Hooks + Lightweight Scheduler)

- **Pipeline hooks** — extend `_process_audio()` with post-processing steps after summarisation
- **Lightweight scheduler** — single asyncio background task ticking every 60s for time-based triggers
- **Pre-computed analytics** — `meeting_analytics` table updated on meeting completion + periodic refresh
- **Notification dispatcher** — thin `src/notifications/` module with three backends (in-app, macOS, external)
- **Recurring series** — `meeting_series` table with hybrid detection (calendar primary, heuristic fallback)

### Integration Flow

```
Meeting completes (pipeline)
    ├─ Extract action items → save to DB
    ├─ Detect/link series → update meeting.series_id
    ├─ Refresh analytics → update daily/weekly rows
    └─ Notify: 'meeting_complete'

Scheduler tick (every 60s)
    ├─ Upcoming meeting in ≤15 min?
    │   └─ Generate prep briefing (uses series history + action items)
    │       └─ Notify: 'prep_briefing' (macos + in_app)
    ├─ Any action items with reminder_at ≤ now?
    │   └─ Notify: 'reminder'
    └─ Any action items past due_date?
        └─ Notify: 'overdue'

Scheduler tick (every 6h)
    └─ Recompute current week/month analytics
        └─ Load score > threshold?
            └─ Notify: 'health_alert'

Scheduler tick (daily, 2 AM)
    └─ Run heuristic series detection on unlinked meetings
        └─ New series found?
            └─ Notify: 'series_detected'
```

---

## Data Model

### New Tables

```sql
CREATE TABLE meeting_series (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    calendar_series_id TEXT,
    detection_method TEXT NOT NULL,   -- 'calendar', 'heuristic', 'manual'
    typical_attendees_json TEXT,
    typical_day_of_week INTEGER,      -- 0=Mon..6=Sun
    typical_time TEXT,                -- HH:MM
    typical_duration_minutes INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE meetings ADD COLUMN series_id TEXT REFERENCES meeting_series(id);

CREATE TABLE action_items (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id),
    title TEXT NOT NULL,
    description TEXT,
    assignee TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open, in_progress, done, cancelled
    priority TEXT DEFAULT 'medium',       -- low, medium, high, urgent
    due_date TEXT,
    reminder_at TEXT,
    source TEXT NOT NULL DEFAULT 'extracted',  -- extracted, manual
    extracted_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE meeting_analytics (
    id TEXT PRIMARY KEY,
    period_type TEXT NOT NULL,        -- 'daily', 'weekly', 'monthly'
    period_start TEXT NOT NULL,
    total_meetings INTEGER DEFAULT 0,
    total_duration_minutes INTEGER DEFAULT 0,
    total_words INTEGER DEFAULT 0,
    unique_attendees INTEGER DEFAULT 0,
    recurring_ratio REAL DEFAULT 0,
    action_items_created INTEGER DEFAULT 0,
    action_items_completed INTEGER DEFAULT 0,
    busiest_hour INTEGER,
    computed_at TEXT NOT NULL,
    UNIQUE(period_type, period_start)
);

CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,               -- 'reminder', 'prep_briefing', 'overdue', 'health_alert'
    reference_id TEXT,
    channel TEXT NOT NULL,            -- 'in_app', 'macos', 'webhook', 'email'
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, sent, failed, dismissed
    scheduled_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE prep_briefings (
    id TEXT PRIMARY KEY,
    meeting_id TEXT,
    series_id TEXT REFERENCES meeting_series(id),
    content_markdown TEXT NOT NULL,
    attendees_json TEXT,
    related_meeting_ids_json TEXT,
    open_action_items_json TEXT,
    generated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
```

### Relationships

```
meeting_series 1──∞ meetings
meetings 1──∞ action_items
meetings 1──∞ prep_briefings (as source)
meeting_series 1──∞ prep_briefings (as target)
action_items ──→ notifications (via reference_id)
prep_briefings ──→ notifications (via reference_id)
```

### Migration

- Schema version: 8 → 9
- All new columns on `meetings` are nullable with defaults (non-breaking)
- New tables are additive
- Existing meetings get `series_id = NULL` until backfill

---

## Module Architecture

### New Source Modules

```
src/
├── action_items/
│   ├── __init__.py
│   ├── extractor.py          # LLM-based extraction from transcripts
│   └── repository.py         # CRUD for action_items table
├── series/
│   ├── __init__.py
│   ├── detector.py           # Hybrid recurring meeting detection
│   └── repository.py         # CRUD for meeting_series table
├── analytics/
│   ├── __init__.py
│   ├── engine.py             # Compute & refresh analytics
│   └── repository.py         # Read/write meeting_analytics table
├── prep/
│   ├── __init__.py
│   ├── briefing.py           # Generate prep briefings via LLM
│   └── repository.py         # CRUD for prep_briefings table
├── notifications/
│   ├── __init__.py
│   ├── dispatcher.py         # Route notifications to channels
│   ├── channels/
│   │   ├── __init__.py
│   │   ├── in_app.py         # WebSocket push via existing event system
│   │   ├── macos.py          # osascript notifications
│   │   └── external.py       # Webhook + email
│   └── repository.py         # Notification log CRUD
└── scheduler.py              # Single asyncio background loop
```

### Pipeline Extension

In `src/main.py` `_process_audio()`:

```
existing: Transcribe → Diarise → Summarise → Write outputs
     new: ... → Extract action items → Detect series → Refresh analytics → Notify
```

New steps are appended sequentially. If any fails, it logs the error but does NOT fail the pipeline.

---

## Feature 1: Recurring Meeting Detection & Series

### Detection Strategies

**Calendar-driven (primary):**

- Check if calendar event has a recurrence/series ID from EventKit
- All meetings sharing the same calendar series ID auto-link to a `meeting_series`
- Series metadata derived from the calendar event

**Heuristic fallback:**

- Runs daily for meetings with no `series_id`
- Clustering criteria (all must match):
  - > =60% attendee overlap (by email, normalized)
  - Same day-of-week (+-1 day tolerance)
  - Similar time slot (+-1 hour)
  - Title similarity >=0.7 (fuzzy match via `difflib.SequenceMatcher`)
- Minimum 3 meetings to form a series
- Confidence score attached

**Manual:**

- User creates a series and links meetings
- Or marks two meetings as "same series"

### Trend Analysis (per series)

- Duration trend — getting longer/shorter
- Attendance trend — more/fewer people
- Action item velocity — created vs completed between occurrences
- Topic drift — summary tag comparison across instances
- Cadence health — on schedule or skipped/rescheduled

### Backfill

On first run, heuristic detector runs against all existing meetings. One-time, non-blocking.

---

## Feature 2: Meeting Insights Panel

### Pre-computed Metrics (per period: daily/weekly/monthly)

| Metric                   | Computation                          |
| ------------------------ | ------------------------------------ |
| `total_meetings`         | COUNT in period                      |
| `total_duration_minutes` | SUM(duration_seconds / 60)           |
| `total_words`            | SUM(word_count)                      |
| `unique_attendees`       | COUNT DISTINCT across attendees_json |
| `recurring_ratio`        | series_id NOT NULL / total           |
| `action_items_created`   | COUNT created in period              |
| `action_items_completed` | COUNT completed in period            |
| `busiest_hour`           | MODE of started_at hour              |

### Live-Computed Metrics

**Meeting load score:** `this_week.total_duration / rolling_4_week_avg_duration`

- < 0.8: "Light week"
- 0.8-1.2: "Normal"
- 1.2-1.5: "Heavy"
- > 1.5: "Overloaded"

**Week-over-week / month-over-month deltas:** percentage change in meetings, duration, words

**Most-met people:** aggregation across attendees_json, ranked by frequency, top 10

**Busiest day breakdown:** meetings grouped by day-of-week, count + avg duration

### Health Indicators

Templated strings built server-side:

- "You had **12 meetings** this week — **30% more** than your 4-week average"
- "Your busiest day was **Tuesday** with 4.5 hours"
- "You have **3 overdue action items** from last week"
- "**Weekly standup** is trending 15 min longer over the past month"

### Computation Triggers

- After each meeting completes: update daily/weekly/monthly rows
- Every 6 hours: recompute current week/month
- On-demand via API

### UI Layout

```
+--------------------------------------------------+
|  Insights                     [This Week v]      |
+----------+----------+----------+----------------+
| Meetings | Hours    | Load     | Action Items   |
| 12 ^30%  | 8.5 ^15%| Heavy    | 5 open, 3 done |
+----------+----------+----------+----------------+
|  Duration Trend (line chart, 8 weeks)            |
+--------------------------------------------------+
|  Busiest Days          |  Most-Met People        |
|  ##### Tue (4.5h)      |  1. Alice (8 meetings)  |
|  ####  Wed (3.2h)      |  2. Bob (6 meetings)    |
|  ###   Thu (2.1h)      |  3. Carol (5 meetings)  |
+--------------------------------------------------+
|  Health Alerts                                   |
|  - 30% more meetings than your 4-week average   |
|  - Weekly standup trending 15 min longer         |
|  - 3 overdue action items                        |
+--------------------------------------------------+
```

Period selector: This Week, Last Week, This Month, Last Month, Last 30 Days.

---

## Feature 3: Action Item Extraction & Tracking

### Extraction

**When:** After summarisation completes in the pipeline.

**How:** Same LLM backend (Claude/Ollama) with focused extraction prompt requesting:

- title (imperative verb)
- assignee (name or "unassigned")
- due_date (if mentioned)
- priority (inferred from urgency)
- extracted_text (exact quote)

**Post-processing:**

- Assignee names normalized against speaker mappings + attendee data
- Duplicates detected: >=0.85 title similarity to existing open item: flagged, not auto-created
- Items created with `source = 'extracted'`, `status = 'open'`

**Fallback:** If LLM returns empty/malformed JSON, retry once with simpler prompt. If still failing, skip gracefully.

### Lifecycle

```
open -> in_progress -> done
open -> cancelled
in_progress -> done
in_progress -> cancelled
done -> open (reopen)
```

Status transitions validated server-side. `completed_at` set/cleared automatically.

### Carry-Forward

When a recurring meeting occurs (same series), prep briefing surfaces open/in-progress items from previous series meetings. If an item is mentioned again (semantic similarity >=0.8), the system links it to the new meeting without creating a duplicate.

### Manual CRUD

- Create from scratch (any meeting or standalone)
- Edit all fields (title, description, assignee, due date, priority)
- Bulk-update status
- Set/change reminder datetime

### UI

```
+--------------------------------------------------+
|  Action Items                [All v] [+ New]     |
+--------------------------------------------------+
|  Filter: [Status v] [Assignee v] [Due v]         |
+--------------------------------------------------+
|  o Draft Q2 roadmap proposal                     |
|    Alice - Due Apr 25 - High - Weekly Planning   |
|  @ Review security audit findings                |
|    Me - Due Apr 22 - Urgent - 1:1 with Bob       |
|  * Send updated timeline to client        Done   |
|    Me - Completed Apr 18 - Client Sync           |
+--------------------------------------------------+
```

---

## Feature 4: Meeting Preparation Mode

### Trigger

Scheduler checks every 60s for calendar events starting within configurable window (default: 15 min).

### Context Gathered

| Source            | Data                                      |
| ----------------- | ----------------------------------------- |
| Series history    | Last 3 meetings: summaries, key decisions |
| Open action items | Open/in-progress items for attendees      |
| Attendee history  | Last 5 meetings with each attendee        |
| Topic continuity  | Tags from previous series meetings        |

### LLM Briefing Generation

Prompt produces:

1. Key context (what was discussed last time, decisions made)
2. Outstanding items (action items for follow-up)
3. Suggested talking points (based on patterns and open items)
4. People context (recent discussions with each attendee)

Target: under 500 words, direct and useful.

### Caching

- TTL: `expires_at = meeting_start + 1 hour`
- Stale briefings not shown if meeting rescheduled/cancelled
- Force-regenerate via API

### Notification Flow

```
Scheduler detects upcoming meeting (T-15 min)
    -> Check: briefing exists and not expired?
    -> No -> generate -> save -> notify (macos + in_app)
    -> macOS notification with deep link -> opens /prep/{meeting_id}
```

### Fallback Without Calendar

Manual only: user clicks "Prepare" in calendar view. Same generation logic, just not auto-triggered.

### UI

```
+--------------------------------------------------+
|  Prep: Weekly Planning                           |
|  Today 2:00 PM - Alice, Bob, Carol    [Refresh]  |
+--------------------------------------------------+
|  LAST TIME (Apr 13)                              |
|  Discussed Q2 roadmap priorities. Agreed to      |
|  focus on mobile-first. Bob raised concerns.     |
+--------------------------------------------------+
|  FOLLOW UP (3 open items)                        |
|  o Draft Q2 roadmap proposal - Alice - Due Apr 25|
|  o Share mobile wireframes - Bob - Due Apr 22    |
|  o Get budget approval - Me - Overdue            |
+--------------------------------------------------+
|  SUGGESTED TALKING POINTS                        |
|  - Q2 roadmap: Alice's proposal should be ready  |
|  - Mobile wireframes: check Bob's progress       |
|  - Budget: escalate if still blocked             |
+--------------------------------------------------+
|  PEOPLE CONTEXT                                  |
|  Alice - also discussed hiring in 1:1 (Apr 17)   |
|  Bob - raised infra concerns in retro (Apr 15)   |
+--------------------------------------------------+
```

---

## Feature 5: Smart Notifications

### Dispatcher

Single entry point: `notify(type, title, body, reference_id, channels, priority, dedupe_window_minutes)`

- Deduplication: same type + reference_id + channel within window: skip
- Priority routing: high -> all channels; low -> in_app only; normal -> user preferences
- All notifications logged to `notifications` table

### Channels

**In-app:** WebSocket event push + badge count + notification drawer

**macOS:** `osascript display notification` with deep links via custom URL scheme (`meetingmind://`)

**External:**

- Webhook: POST JSON (Slack-compatible format or generic). User-configured URL.
- Email: SMTP with TLS. Minimal HTML template. Max 3/day throttle. High-priority only.

### Notification Types

| Type                | Trigger                       | Default Channels       | Priority |
| ------------------- | ----------------------------- | ---------------------- | -------- |
| `prep_briefing`     | Meeting in <=15 min           | in_app, macos          | normal   |
| `reminder`          | `reminder_at` reached         | in_app, macos          | normal   |
| `overdue`           | `due_date` passed, still open | in_app, macos, webhook | high     |
| `health_alert`      | Load score > 1.5              | in_app                 | low      |
| `meeting_complete`  | Pipeline finished             | in_app                 | low      |
| `series_detected`   | New pattern found             | in_app                 | low      |
| `action_item_carry` | Item mentioned again          | in_app, macos          | normal   |

### UI

Sidebar badge (unread count) + slide-out drawer with grouped notifications (Now, Today, Yesterday). Each notification has primary action (navigate) and optional secondary action (e.g., "Mark done").

---

## API Routes

### Action Items

```
GET    /api/action-items
GET    /api/action-items/{id}
POST   /api/action-items
PATCH  /api/action-items/{id}
DELETE /api/action-items/{id}
GET    /api/meetings/{id}/action-items
```

### Series

```
GET    /api/series
GET    /api/series/{id}
POST   /api/series
PATCH  /api/series/{id}
DELETE /api/series/{id}
POST   /api/series/{id}/meetings
GET    /api/series/{id}/trends
```

### Analytics

```
GET    /api/analytics/summary
GET    /api/analytics/trends
GET    /api/analytics/people
GET    /api/analytics/health
POST   /api/analytics/refresh
```

### Prep

```
GET    /api/prep/upcoming
GET    /api/prep/{meeting_id}
POST   /api/prep/{meeting_id}/generate
```

### Notifications

```
GET    /api/notifications
PATCH  /api/notifications/{id}
GET    /api/notifications/unread-count
POST   /api/notifications/settings
```

---

## Configuration

All new config under new top-level keys (existing keys untouched):

```yaml
action_items:
  auto_extract: true
  default_reminder_before_due: "1d"
  duplicate_threshold: 0.85

series:
  heuristic_enabled: true
  min_meetings_for_series: 3
  attendee_overlap_threshold: 0.6
  title_similarity_threshold: 0.7
  time_tolerance_hours: 1
  day_tolerance: 1

analytics:
  refresh_interval_hours: 6
  rolling_window_weeks: 4
  health_alert_threshold: 1.5

notifications:
  enabled: true
  channels:
    in_app: true
    macos: true
    webhook:
      enabled: false
      url: ""
      format: "slack"
    email:
      enabled: false
      smtp_host: ""
      smtp_port: 587
      smtp_user: ""
      smtp_password: ""
      from_address: ""
      to_address: ""
      max_per_day: 3
  reminders:
    default_before_due: "1d"
    overdue_check_interval: "6h"

prep:
  lead_time_minutes: 15
  auto_generate: true
  max_context_meetings: 3
  max_attendee_history: 5
  briefing_ttl_hours: 2
```

---

## Graceful Degradation

| Condition                   | Behaviour                                                                       |
| --------------------------- | ------------------------------------------------------------------------------- |
| Calendar disabled           | No auto prep, no calendar series. Heuristic + manual still work                 |
| LLM unavailable             | Action extraction + prep briefing skipped. Analytics + notifications still work |
| Webhook/email misconfigured | External channel fails silently. In-app + macOS unaffected                      |
| No meetings yet             | Empty state UI, no series detected                                              |
| Single meeting in series    | "Not enough data" instead of misleading charts                                  |

---

## New UI Views

```
ui/src/components/
├── action-items/
│   ├── ActionItemList.tsx
│   ├── ActionItemCard.tsx
│   └── ActionItemForm.tsx
├── series/
│   ├── SeriesList.tsx
│   ├── SeriesDetail.tsx
│   └── SeriesLinkModal.tsx
├── analytics/
│   ├── InsightsPanel.tsx
│   ├── TrendChart.tsx
│   ├── PeopleRanking.tsx
│   └── HealthScore.tsx
├── prep/
│   ├── PrepBriefing.tsx
│   └── PrepNotification.tsx
└── notifications/
    ├── NotificationPanel.tsx
    ├── NotificationBadge.tsx
    └── NotificationSettings.tsx
```

Sidebar additions: **Action Items**, **Insights**, **Notifications** (with badge). Series and Prep accessed contextually from meeting detail and calendar views.

---

## Implementation Order

1. **Recurring Meeting Detection** — data model foundation, series table, detection logic
2. **Meeting Insights Panel** — analytics engine, pre-computation, dashboard UI
3. **Action Item Extraction & Tracking** — extraction pipeline, CRUD, UI
4. **Meeting Preparation Mode** — briefing generation, caching, auto-trigger
5. **Smart Notifications** — dispatcher, channels, scheduler integration

This order builds dependencies bottom-up: series informs analytics and prep; action items feed prep and notifications; notifications ties everything together.
