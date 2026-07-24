# Notifications Redesign — Design Spec

**Date:** 2026-07-24
**Status:** Approved (design); pending written-spec review → implementation plan
**Branch:** `worktree-notifications-redesign`
**Author:** design lead (with product owner)

---

## 1. Context & Problem

Context Recall's notifications feature has accumulated **99+ unread items** that clog the badge and spam the device with macOS banners. The subsystem is cleanly layered on paper (`producers → NotificationDispatcher → channels → notifications table → REST → React panel`) but is simultaneously **too noisy** (unbounded re-fires, un-throttled banners) and **not useful enough** (the only thing that notifies is overdue nagging; there is no positive, actionable, or click-through event, and no controls to tune it).

This spec defines a phased redesign that (a) stops the pileup at its source, (b) gives the user real control and one-tap management, and (c) turns the panel into a useful, integrated inbox.

## 2. Root Causes (verified against the code)

Ranked by causal weight. All references are to files on `main` at the time of writing.

1. **Overdue/reminder sweep regenerates notifications forever.** `_check_reminders` (`src/api/server.py:504`) is registered to run **every 60 seconds** (`server.py:471-475`, interval hardcoded to `60`). The _only_ throttle is a 60-minute rolling dedup window (`NotificationDispatcher.notify`, `src/notifications/dispatcher.py:47`). There is no permanent "already notified" record, so every chronically-overdue action item re-creates fresh rows **every hour, indefinitely** (~48 rows/item/day across in-app + macOS).
2. **`reminder_at` is never cleared after firing.** `list_due_reminders` (`src/action_items/repository.py:250`) selects any open/in-progress item with `reminder_at <= now`, and nothing resets `reminder_at` after the reminder is sent — so the same item re-qualifies on every sweep. `list_overdue` / `list_due_reminders` also return the **entire backlog with no LIMIT**.
3. **The config lies about cadence.** `NotificationsConfig.overdue_check_interval = "6h"` and `default_reminder_before_due = "1d"` (`src/utils/config.py:355,354`) are **never read**; the real cadence is the hardcoded 60s.
4. **Badge double-counts and never structurally clears.** `count_unread` (`src/notifications/repository.py:133`) counts **every `status='sent'` row across all channels**, so each event contributes ~2× (in-app + macOS). `create` writes **one row per channel** (`repository.py:18`). Dismiss (`repository.py:141`) only flips status; the next sweep re-inflates the count.
5. **No retention.** Nothing prunes the `notifications` table (`src/db/database.py:198-215`); it is append-only for the life of the DB. There is no `created_at` index despite every list/query ordering or filtering on it.
6. **Automations bypass the whole system.** The automation `notify` action (`src/automations/executor.py:122-126`) calls `macos_send()` **directly** — no dedup, no rate-limit, no DB row, no config gate, undismissable.
7. **Phantom email cap.** `email.max_per_day` (`config.py:332`) is surfaced in Settings but **never enforced** in `send_email` (`src/notifications/channels/external.py:48`). The noisy macOS channel has no cap at all.
8. **macOS "success" is faked.** `_send_channel` returns `True` for macOS before checking the `osascript` return code (`dispatcher.py:102-103`), so a failed banner is logged as `sent`.
9. **The UI cannot drain the backlog.** The panel fetches 50 rows with no pagination (`ui/src/components/notifications/NotificationPanel.tsx:29`) and offers **only single per-row dismiss** — there is no mark-all-read / clear-all endpoint (`src/api/routes/notifications.py`). `type` / `reference_id` are returned but never rendered or made clickable.

## 3. Goals & Non-Goals

**Goals**

- Stop the 99+ pileup at the source; make dismissal _stick_.
- Give the user granular, working controls (per-type, quiet hours, rate limits, sound).
- Reserve interruptions (macOS banner + sound) for events the user actually chose.
- Make the panel a triageable, click-through inbox that resolves as the user acts.
- Route **every** producer (including automations) through one governed dispatcher.
- Add genuinely useful events (meeting processed / failed, prep ready, high-signal insights).

**Non-Goals**

- Not a compliance audit log — notification history is convenience data and may be pruned.
- No new external channels beyond the existing in-app / macOS / webhook / email.
- No cross-device sync or push infrastructure.
- No change to how action items, insights, or calendar data themselves are produced — only to how they _notify_.

## 4. Decisions Locked (product owner)

| Decision                           | Choice                                                                        |
| ---------------------------------- | ----------------------------------------------------------------------------- |
| Ambition                           | **Phased**: fix & streamline first, then extend into an integrated center     |
| Backlog                            | **Clear + silence now** — one-time purge of the 99+, mute banners immediately |
| Active interruption (banner+sound) | **Meeting processed, Upcoming-meeting prep, Insights & automations**          |
| Overdue/reminders                  | **No banner** — in-app + daily digest only                                    |
| Delivery philosophy                | **Hybrid** — urgent live, everything else digested                            |

**Design defaults chosen for the remaining open questions (changeable):**

- Chronic overdue: notify **once on state transition** + a **daily digest**; never hourly.
- Retention: keep **30 days**; auto-prune dismissed after **7 days**; hard cap **500 rows** (delete oldest beyond).
- Quiet hours: **22:00–08:00** local; **macOS sound off** by default.

## 5. Target Design

### 5.1 Notification model & lifecycle

A notification becomes a **single persistent record per logical event** with a real lifecycle:

```
created ──▶ unread ──▶ read ──▶ dismissed ──▶ (pruned)
                 └── (snoozed → unread later)
```

- **One row per event**, not per channel. The set of channels it was delivered on is stored on the row (`channels`).
- `read` is distinct from `dismissed`: opening the panel marks visible items **read** (badge → 0) without destroying them; dismiss/clear remove them from the default view.
- The badge counts **unread only**.

### 5.2 Dispatch pipeline (governed gates)

`NotificationDispatcher.notify(...)` becomes the single chokepoint. Order of gates:

1. **Master + per-type mute** — drop if `notifications.enabled` is false or the event `type` is muted.
2. **Dedup (DB-level)** — compute a stable `dedup_key`; `INSERT … ON CONFLICT(dedup_key) DO NOTHING`. Replaces the racy SELECT-then-INSERT (`repository.py:56`).
3. **Rate limits** — per-type and global `max_per_hour`; enforce `email.max_per_day`; add a macOS cap.
4. **Priority → channel routing** — resolve channels from config (`macos_min_priority`, `external_min_priority`) rather than the hardcoded map (`dispatcher.py:68`).
5. **Quiet hours** — during quiet hours, suppress _interruptive_ channels (macOS banner/sound); still record in-app. Sound is opt-in globally.
6. **Deliver** — fan out to resolved channels with `asyncio.gather`; check each channel's real success (fix `osascript` return code); persist the single row with the delivered-channel set and accurate status.

### 5.3 Producers & event taxonomy

Every producer calls `dispatcher.notify(type=…, priority=…, reference_type=…, reference_id=…)`. Event classes and default routing:

| Type                             | Trigger                                          | Default routing                   | Interrupts?               |
| -------------------------------- | ------------------------------------------------ | --------------------------------- | ------------------------- |
| `meeting_processed`              | Recording transcribed, notes ready               | banner + in-app                   | ✅                        |
| `meeting_failed`                 | Pipeline/diarisation error                       | banner + in-app (always, high)    | ✅ always                 |
| `prep_ready`                     | Calendar prep/briefing ready; meeting imminent   | banner + in-app                   | ✅                        |
| `insight`                        | High-signal insight (tracker hit / flagged risk) | banner + in-app                   | ✅ (gated to high-signal) |
| `automation`                     | Automation `notify` action matched               | banner + in-app                   | ✅ (per-rule cooldown)    |
| `task_overdue` / `task_reminder` | Action item newly overdue / due                  | in-app only + digest              | ❌ never banner           |
| `digest`                         | Scheduled daily rollup                           | in-app (+ optional single banner) | configurable              |

`meeting_processed` / `meeting_failed` / `insight` are **new first-class persisted notifications** (today they are transient EventBus frames or nonexistent). A failed pipeline currently leaves **no durable trace** — this is the highest-value new event.

### 5.4 Data model & migration (schema v24 → v25)

**`notifications` table** — add columns + indexes, collapse to one-row-per-event:

| Column                | Change                                   | Purpose                                                                  |
| --------------------- | ---------------------------------------- | ------------------------------------------------------------------------ |
| `read_at REAL`        | **new**                                  | real read state distinct from dismissed                                  |
| `dedup_key TEXT`      | **new**, partial `UNIQUE` where not null | atomic DB-level dedup                                                    |
| `group_key TEXT`      | **new**                                  | grouping/threading in UI                                                 |
| `reference_type TEXT` | **new**                                  | click-through target kind (`meeting`/`action_item`/`insight`/`calendar`) |
| `priority TEXT`       | **new**                                  | drives routing + UI severity                                             |
| `channels TEXT`       | **new** (csv/json)                       | channels delivered on (replaces one-row-per-channel)                     |
| `snoozed_until REAL`  | **new**                                  | snooze support                                                           |
| index on `created_at` | **new**                                  | list/query performance                                                   |

Status vocabulary: `unread | read | dismissed` (plus `failed` for delivery audit). Existing rows migrate: `status='sent'` → `unread`, `dismissed` → `dismissed`; `read_at` null.

**`action_items` table** — add transition-tracking so overdue notifies once:

| Column                     | Purpose                                                   |
| -------------------------- | --------------------------------------------------------- |
| `overdue_notified_at REAL` | set when the item first flips to overdue; gates re-notify |

Reminders: clear `reminder_at` (or set a `reminder_sent_at`) after firing so `list_due_reminders` stops re-selecting it.

### 5.5 Config schema (`NotificationsConfig`)

Extend `src/utils/config.py:348`:

```python
@dataclass
class NotificationsConfig:
    enabled: bool = True
    # channels
    in_app: bool = True
    macos: bool = True
    macos_sound: bool = False           # NEW — sound off by default
    webhook: WebhookChannelConfig = ...
    email: EmailChannelConfig = ...      # max_per_day now ENFORCED
    # per-type mute (event type -> enabled)
    muted_types: list[str] = field(default_factory=list)   # NEW
    # routing
    macos_min_priority: str = "normal"   # NEW
    external_min_priority: str = "high"  # NEW
    # rate limits
    max_per_hour: int = 12               # NEW (global)
    per_type_max_per_hour: dict = ...    # NEW
    # quiet hours
    quiet_hours_enabled: bool = True     # NEW
    quiet_start: str = "22:00"           # NEW
    quiet_end: str = "08:00"             # NEW
    # digest
    task_digest: str = "daily"           # NEW: off | daily
    digest_time: str = "08:00"           # NEW
    # cadence (wire the dead fields, real units)
    overdue_recheck_minutes: int = 360   # replaces unused overdue_check_interval
    dedup_window_minutes: int = 60
    # retention
    retention_days: int = 30             # NEW
    dismissed_retention_days: int = 7    # NEW
    max_rows: int = 500                  # NEW
```

Dead/unused fields (`overdue_check_interval`, `default_reminder_before_due`) are either wired to real behavior or removed with a migration of `config.yaml`.

### 5.6 API surface (`src/api/routes/notifications.py`)

Add:

- `POST /api/notifications/read-all` — mark all (or a filtered set) read.
- `POST /api/notifications/clear-all` — dismiss all (or a filtered set).
- `PATCH /api/notifications/{id}` — extend to support `read` / `snooze` (with `snoozed_until`) in addition to `dismiss`.
- `GET /api/notifications` — add `type` filter, cursor/offset pagination, default `exclude_dismissed=true`.
- `GET /api/notifications/preferences` + `PATCH` — read/write the per-type + quiet-hours prefs (or fold into existing config route `src/api/routes/config.py`).

### 5.7 UI / UX (`ui/src/components/notifications/`)

- **Header:** title + unread count, quiet-hours indicator, **Mark all read** + **Clear all** bulk actions.
- **Filter chips:** All / Meetings / Failures / Insights / Calendar / Tasks with counts.
- **Digest card** pinned at top: "N due today · M overdue" → opens Action Items.
- **Rows:** type icon + category color, priority **severity stripe** for high/failures, unread dot, relative time, hover **snooze + dismiss**, and **click-through** to `reference_type`/`reference_id`.
- **Sections:** Today / Earlier buckets by `created_at`.
- **Empty state:** "You're all caught up."
- **Badge:** single source of truth (derive from react-query cache; optimistic decrement on read/dismiss), fixing the WS-`+1` vs 30s-poll drift (`appStore.ts`).
- **Settings:** per-type toggles (banner / in-app / digest / off), quiet-hours, sound, rate caps — extend the Notifications section (`Settings.tsx:1874`).

A published interactive mockup accompanies this spec (the redesigned panel on the app's real OKLCH tokens).

## 6. Behavioral Rules

- **Re-notify (overdue):** notify **once** when an item transitions to overdue (`overdue_notified_at` null → now). Never re-notify hourly. The item's ongoing state is reflected only in the daily digest.
- **Re-notify (reminder):** fire once at `reminder_at`, then clear it.
- **Digest:** one `digest` notification per day at `digest_time` summarizing due/overdue counts, linking to Action Items. `task_digest: off` disables it.
- **Quiet hours:** within `[quiet_start, quiet_end)`, macOS banners/sound are suppressed; the notification is still recorded in-app and appears when the user next opens the panel. Urgent `meeting_failed` may optionally override (default: respect quiet hours).
- **Rate limits:** per-type and global `max_per_hour`; `email.max_per_day` enforced; excess is coalesced/dropped with a log — never silently unbounded.
- **Dedup:** `dedup_key = f"{type}:{reference_id}:{bucket}"` where `bucket` is the state/day that should collapse duplicates; DB `UNIQUE` guarantees idempotency across retries, reprocessing, and multi-channel.
- **Retention:** scheduled prune — dismissed older than `dismissed_retention_days`, anything older than `retention_days`, and oldest rows beyond `max_rows`. Mirrors `cleanup_old_meetings` (`db/repository.py`).

## 7. Phasing

### Phase 0 — Immediate relief (ship first, minimal)

- **Purge migration:** mark all existing `notifications` rows `dismissed` → badge to 0.
- **Stop the bleed:** guard the overdue re-fire (respect `overdue_notified_at` / raise the effective interval) and default macOS **sound off**, so the spam stops even before Phase 1 fully lands.

### Phase 1 — Streamline & make correct (the "fix")

- Kill the re-fire engine: transition-based overdue notify; clear `reminder_at`; wire real cadence.
- DB-level dedup (`dedup_key` + `ON CONFLICT`); one-row-per-event; `created_at` index.
- Lifecycle + retention: `read` state, `read_at`, prune task, accurate badge.
- Bulk API + UI: read-all / clear-all, mark-read-on-open, pagination, hide dismissed.
- Controls that work: per-type mute, quiet hours, real rate caps (fix phantom `max_per_day`), routing, sound opt-in; remove/wire dead config.
- Route automations through the dispatcher + per-rule `notify_cooldown_minutes`.
- Correctness polish: `asyncio.gather` sends, real `osascript` status, single dispatcher instance, single badge source.

### Phase 2 — Useful & integrated (the "extend")

- New first-class events: `meeting_processed`, `meeting_failed`, gated `insight`, `prep_ready`.
- Inbox UX: grouping + filter chips + Today/Earlier, click-through, type icons, severity styling.
- Daily digest.
- Actionability: snooze / mark-done / complete-from-notification.
- Unify the three surfaces (toast / panel / macOS-native) under one dedup + state model.

## 8. Testing Strategy

- **Migration:** v24→v25 forward test (columns, indexes, data backfill of status/read_at); idempotency; purge correctness.
- **Dispatcher:** each gate in isolation (mute, dedup ON CONFLICT, rate-limit, routing, quiet-hours suppression, gather delivery, real macOS status).
- **Producers:** overdue notifies once per transition (not per sweep); reminder clears; automations go through dispatcher and honor cooldown/toggles; new meeting/insight/prep events fire with correct type/priority/reference.
- **Retention:** prune windows + hard cap; dismissed vs age vs count.
- **API:** read-all / clear-all / snooze / filter / pagination; preferences round-trip.
- **UI (vitest):** bulk actions, mark-read-on-open, badge single-source, filter chips, click-through navigation, empty state.
- Follow repo TDD conventions; keep Python + UI suites green.

## 9. Risks & Tradeoffs

- **A real `read` state** adds a state axis to model/UI/migration — but is non-destructive and the correct fix for "seen ≠ deleted".
- **DB-level dedup** guarantees correctness but fixes the dedup key shape; tunable windows become secondary. Acceptable — correctness first.
- **Digests can bury an urgent single item** — mitigated by keeping urgent classes (failures) always-individual and never digest-only.
- **Quiet hours could hide a genuinely urgent failure** — default respects quiet hours; a per-type "override quiet hours" flag is available for `meeting_failed` if desired.
- **Migration touches a shared table** — Phase 0 purge is destructive to unread state (intended, user chose "clear"); history is archived as dismissed, not deleted.

## 10. Out of Scope

- New channels (SMS, Slack app beyond webhook, mobile push).
- Notification analytics/reporting.
- Reworking action-item, insight, or calendar domain logic beyond their notify hooks.

## 11. Appendix — File Impact Map

| Area      | Files                                                                                                                                                                                                                                                      |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dispatch  | `src/notifications/dispatcher.py`, `channels/{macos,external,in_app}.py`, `channels/__init__.py`                                                                                                                                                           |
| Storage   | `src/notifications/repository.py`, `src/db/database.py` (migration v25)                                                                                                                                                                                    |
| Producers | `src/api/server.py` (`_check_reminders`, new event hooks), `src/action_items/repository.py`, `src/automations/executor.py`                                                                                                                                 |
| Config    | `src/utils/config.py`, `config.example.yaml`, `src/api/routes/config.py`                                                                                                                                                                                   |
| API       | `src/api/routes/notifications.py`                                                                                                                                                                                                                          |
| UI        | `ui/src/components/notifications/{NotificationPanel,NotificationBadge}.tsx`, `ui/src/hooks/useNotifications.ts`, `ui/src/stores/appStore.ts`, `ui/src/lib/{api,types}.ts`, `ui/src/components/settings/Settings.tsx`, `ui/src/components/common/Toast.tsx` |
| Tests     | `tests/test_notifications.py`, `tests/test_db_migration_v25.py` (new), `tests/test_automations_executor.py`, UI `__tests__`                                                                                                                                |
