"""One-time seeding of tailored starter insights + automation rules."""

import logging

logger = logging.getLogger("contextrecall.insights.seed")

SEED_VERSION = 1
_MARKER_KEY = "insights_seed_version"

_SEED_INSIGHTS = [
    {
        "name": "Client Call Details",
        "prompt": "Extract the key delivery details discussed with the client.",
        "fields": [
            {"key": "go_live_date", "label": "Go-live date", "type": "date"},
            {"key": "blockers", "label": "Blockers", "type": "list"},
            {"key": "risks", "label": "Risks", "type": "list"},
            {"key": "decisions", "label": "Decisions", "type": "list"},
            {"key": "owner_next_step", "label": "Owner / next step", "type": "text"},
        ],
    },
    {
        "name": "Standup Snapshot",
        "prompt": "Summarise the standup status across projects.",
        "fields": [
            {"key": "project_status", "label": "Per-project status", "type": "list"},
            {"key": "overdue_count", "label": "Overdue task count", "type": "number"},
            {"key": "absences", "label": "Absences & coverage", "type": "list"},
            {"key": "deadlines", "label": "Key deadlines", "type": "list"},
        ],
    },
    {
        "name": "Discovery Notes",
        "prompt": "Capture the discovery outcomes.",
        "fields": [
            {"key": "requirements", "label": "Requirements", "type": "list"},
            {"key": "open_questions", "label": "Open questions", "type": "list"},
            {"key": "scope_decisions", "label": "Scope decisions", "type": "list"},
            {"key": "compliance_flags", "label": "Compliance / PCI flags", "type": "text"},
        ],
    },
]

# (title substrings, insight name) — rules trigger on the title, any-match.
_SEED_RULES = [
    ("Client call auto-insight", ["uat", "client", "review"], "Client Call Details"),
    ("Standup auto-insight", ["catchup", "standup"], "Standup Snapshot"),
    ("Discovery auto-insight", ["discovery"], "Discovery Notes"),
]


async def seed_starter_content(meeting_repo, insight_repo, automation_repo) -> bool:
    if await meeting_repo.get_meta(_MARKER_KEY) is not None:
        return False
    name_to_id: dict[str, str] = {}
    for spec in _SEED_INSIGHTS:
        did = await insight_repo.create(
            name=spec["name"],
            prompt=spec["prompt"],
            enabled=True,
            output_mode="structured",
            fields=spec["fields"],
        )
        name_to_id[spec["name"]] = did
    for rule_name, substrings, insight_name in _SEED_RULES:
        did = name_to_id.get(insight_name)
        if not did:
            continue
        conditions = [{"field": "title_contains", "value": s} for s in substrings]
        await automation_repo.create(
            name=rule_name,
            match_mode="any",
            conditions=conditions,
            actions=[{"type": "run_insight", "definition_id": did}],
            enabled=True,
        )
    await meeting_repo.set_meta(_MARKER_KEY, str(SEED_VERSION))
    logger.info("Seeded %d starter insights + %d rules", len(_SEED_INSIGHTS), len(_SEED_RULES))
    return True
