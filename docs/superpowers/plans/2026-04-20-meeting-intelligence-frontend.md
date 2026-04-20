# Meeting Intelligence Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add frontend views for action items, insights panel, notifications, prep briefings, and series management to the MeetingMind Tauri/React UI.

**Architecture:** Extends the existing React + React Router + React Query + Zustand + Tailwind stack. Each feature gets its own component directory under `ui/src/components/`. API functions added to `ui/src/lib/api.ts`, types to `ui/src/lib/types.ts`. The sidebar gains new nav items (Action Items, Insights) and a notification bell. No new dependencies -- charts built with Tailwind/HTML following the existing MeetingHeatmap pattern.

**Tech Stack:** React 19 / TypeScript 5.8 / React Router 7 / React Query 5 / Zustand 5 / Tailwind 4 / date-fns 4 / Framer Motion 12

---

## Phase 1: Types and API Client

### Task 1: TypeScript Types

**Files:**

- Modify: `ui/src/lib/types.ts`

- [ ] **Step 1: Add intelligence feature types to end of types.ts**

Add action item, series, analytics, notification, and prep briefing interfaces. Add new WSEvent variants for `notification` and `action_items.extracted`.

- [ ] **Step 2: Commit**

```bash
cd ui && git add src/lib/types.ts
git commit -m "feat(ui): add TypeScript types for intelligence features"
```

### Task 2: API Client Functions

**Files:**

- Modify: `ui/src/lib/api.ts`

- [ ] **Step 1: Add action items API (getActionItems, getMeetingActionItems, createActionItem, updateActionItem, deleteActionItem)**
- [ ] **Step 2: Add series API (getSeries, getSeriesDetail, createSeries, deleteSeries, linkMeetingToSeries, getSeriesTrends)**
- [ ] **Step 3: Add analytics API (getAnalyticsSummary, getAnalyticsTrends, getAnalyticsPeople, getAnalyticsHealth, refreshAnalytics)**
- [ ] **Step 4: Add notifications API (getNotifications, getUnreadCount, dismissNotification)**
- [ ] **Step 5: Add prep API (getUpcomingPrep with 204 handling, getPrepForMeeting, generatePrep)**
- [ ] **Step 6: Commit**

```bash
cd ui && git add src/lib/api.ts
git commit -m "feat(ui): add API client functions for intelligence features"
```

---

## Phase 2: Notification Infrastructure

### Task 3: Notification Badge + Store Update

**Files:**

- Create: `ui/src/components/notifications/NotificationBadge.tsx`
- Modify: `ui/src/stores/appStore.ts`
- Modify: `ui/src/components/layout/Sidebar.tsx`

- [ ] **Step 1: Add unreadNotifications state to appStore, handle notification WSEvent**
- [ ] **Step 2: Create NotificationBadge component (reads count from store)**
- [ ] **Step 3: Add Action Items, Insights nav items and bell icon to Sidebar with NotificationBadge**
- [ ] **Step 4: Commit**

```bash
cd ui && git add src/stores/appStore.ts src/components/notifications/NotificationBadge.tsx src/components/layout/Sidebar.tsx
git commit -m "feat(ui): add notification badge and sidebar nav updates"
```

### Task 4: Notification Panel (Slide-out Drawer)

**Files:**

- Create: `ui/src/components/notifications/NotificationPanel.tsx`
- Modify: `ui/src/App.tsx`

- [ ] **Step 1: Create NotificationPanel with AnimatePresence slide-out, query notifications, dismiss mutation, unread polling**
- [ ] **Step 2: Render NotificationPanel in AppShell, add toggle-notifications CustomEvent listener**
- [ ] **Step 3: Commit**

```bash
cd ui && git add src/components/notifications/NotificationPanel.tsx src/App.tsx
git commit -m "feat(ui): add notification slide-out panel"
```

---

## Phase 3: Action Items View

### Task 5: ActionItemCard Component

**Files:**

- Create: `ui/src/components/action-items/ActionItemCard.tsx`

- [ ] **Step 1: Create ActionItemCard with status toggle (click cycles open/in_progress/done), priority colors, due date display**
- [ ] **Step 2: Commit**

```bash
cd ui && git add src/components/action-items/ActionItemCard.tsx
git commit -m "feat(ui): add ActionItemCard component"
```

### Task 6: ActionItemForm + ActionItemList Page

**Files:**

- Create: `ui/src/components/action-items/ActionItemForm.tsx`
- Create: `ui/src/components/action-items/ActionItemList.tsx`
- Modify: `ui/src/App.tsx`

- [ ] **Step 1: Create ActionItemForm modal (create + edit modes, title/assignee/priority/due_date/description fields)**
- [ ] **Step 2: Create ActionItemList page with status filter buttons, skeleton loading, empty state**
- [ ] **Step 3: Add /action-items route in App.tsx**
- [ ] **Step 4: Commit**

```bash
cd ui && git add src/components/action-items/ src/App.tsx
git commit -m "feat(ui): add Action Items page with list, card, and form"
```

---

## Phase 4: Insights Panel

### Task 7: Insights Panel Components

**Files:**

- Create: `ui/src/components/insights/StatCard.tsx`
- Create: `ui/src/components/insights/TrendChart.tsx`
- Create: `ui/src/components/insights/PeopleRanking.tsx`
- Create: `ui/src/components/insights/HealthAlerts.tsx`
- Create: `ui/src/components/insights/InsightsPanel.tsx`
- Modify: `ui/src/App.tsx`

- [ ] **Step 1: Create StatCard (label, value, optional delta with arrow)**
- [ ] **Step 2: Create TrendChart (Tailwind bar chart using percentage heights, date-fns labels)**
- [ ] **Step 3: Create PeopleRanking (numbered list with meeting counts)**
- [ ] **Step 4: Create HealthAlerts (load label badge + indicator list)**
- [ ] **Step 5: Create InsightsPanel page (period selector, 4-col stat grid, 2-col charts row, health section)**
- [ ] **Step 6: Add /insights route in App.tsx**
- [ ] **Step 7: Commit**

```bash
cd ui && git add src/components/insights/ src/App.tsx
git commit -m "feat(ui): add Insights Panel with stats, trends, people, and health"
```

---

## Phase 5: Prep Briefing and Series

### Task 8: Prep Briefing View

**Files:**

- Create: `ui/src/components/prep/PrepBriefing.tsx`
- Modify: `ui/src/App.tsx`

- [ ] **Step 1: Create PrepBriefing (loads upcoming or per-meeting, ReactMarkdown rendering, generate button, skeleton loading)**
- [ ] **Step 2: Add /prep and /prep/:meetingId routes in App.tsx**
- [ ] **Step 3: Commit**

```bash
cd ui && git add src/components/prep/ src/App.tsx
git commit -m "feat(ui): add Prep Briefing view with markdown rendering"
```

### Task 9: Series Detail View

**Files:**

- Create: `ui/src/components/series/SeriesDetail.tsx`
- Modify: `ui/src/App.tsx`

- [ ] **Step 1: Create SeriesDetail (title, metadata, Tailwind bar chart for duration trend, linked meetings list with links to /meetings/:id)**
- [ ] **Step 2: Add /series/:id route in App.tsx**
- [ ] **Step 3: Commit**

```bash
cd ui && git add src/components/series/ src/App.tsx
git commit -m "feat(ui): add Series Detail view with trend visualization"
```

---

## Phase 6: Integration Points

### Task 10: Meeting Detail -- Action Items Section

**Files:**

- Modify: `ui/src/components/meetings/MeetingDetail.tsx`

- [ ] **Step 1: Add MeetingActionItems section (queries getMeetingActionItems, renders ActionItemCard list)**
- [ ] **Step 2: Commit**

```bash
cd ui && git add src/components/meetings/MeetingDetail.tsx
git commit -m "feat(ui): show action items in meeting detail view"
```

### Task 11: WebSocket Event Integration

**Files:**

- Modify: `ui/src/App.tsx`

- [ ] **Step 1: In onWSEvent, invalidate action-items on action_items.extracted and notifications-unread on notification events**
- [ ] **Step 2: Commit**

```bash
cd ui && git add src/App.tsx
git commit -m "feat(ui): invalidate queries on intelligence WebSocket events"
```

### Task 12: Final Build Verification

- [ ] **Step 1: Run npx tsc --noEmit, fix type errors**
- [ ] **Step 2: Run npm run build, fix build errors**
- [ ] **Step 3: Commit fixes**

```bash
cd ui && git add -A
git commit -m "fix(ui): resolve type and build errors"
```

---

## Summary

| Phase              | Tasks | What Ships                                          |
| ------------------ | ----- | --------------------------------------------------- |
| 1: Types and API   | 1-2   | TypeScript interfaces, all API client functions     |
| 2: Notifications   | 3-4   | Badge, store update, slide-out drawer               |
| 3: Action Items    | 5-6   | Card, form, list page with filters                  |
| 4: Insights        | 7     | Full analytics dashboard with charts                |
| 5: Prep and Series | 8-9   | Prep briefing view, series detail with trends       |
| 6: Integration     | 10-12 | Meeting detail action items, WS events, build check |
