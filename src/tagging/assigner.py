"""
Meeting → client/project assignment.

Two passes:

- **Deterministic** (before summarisation, cheap and offline): attendee
  email domains against client domains, client/project names + aliases
  against the calendar event title, and series inheritance (a meeting
  in a series follows the series' latest assignment — this is how
  recurring schedules teach the app). When a match lands, the matched
  client/project *descriptions* are injected into the summariser prompt
  so the model knows the terminology and context of the account.

- **LLM** (post-processing, only when still unassigned): the summary,
  attendees, and the client/project roster with descriptions go to the
  configured LLM backend, which returns a pick with confidence. Picks
  under ``tagging.min_confidence`` are discarded.

Manual assignments are never overwritten by either pass.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from src.summariser import Summariser
from src.utils.config import SummarisationConfig, TaggingConfig

logger = logging.getLogger("contextrecall.tagging")

# Personal-mail domains never identify a client.
_GENERIC_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "live.co.uk",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
}

ASSIGNMENT_PROMPT = """You are a precise meeting classifier. Given a meeting's details and a
roster of known clients and projects (with descriptions), decide which
client and/or project the meeting belongs to.

Return ONLY a JSON object:
- "client_id": the id of the matching client, or null
- "project_id": the id of the matching project, or null
- "confidence": 0.0-1.0, how certain you are
- "rationale": one short sentence of evidence

Rules:
- Only pick from the roster ids. Never invent ids.
- A project pick implies its client when the project has one.
- If nothing in the roster fits, return nulls with confidence 0."""


@dataclass
class Assignment:
    client_id: str | None = None
    project_id: str | None = None
    confidence: float = 0.0
    method: str = ""  # "domain" | "alias" | "series" | "llm"


def _names_and_aliases(entity: dict) -> list[str]:
    names = [entity.get("name", "")]
    names.extend(entity.get("aliases", []))
    return [n for n in names if n and len(n) >= 3]


def _title_matches(title: str, entity: dict) -> bool:
    for name in _names_and_aliases(entity):
        if re.search(rf"\b{re.escape(name)}\b", title, re.IGNORECASE):
            return True
    return False


def deterministic_assignment(
    roster: dict,
    *,
    attendees: list[dict] | None = None,
    calendar_title: str = "",
    series_assignment: dict | None = None,
) -> Assignment | None:
    """Offline signals, strongest first. Returns None when nothing lands."""
    clients = roster.get("clients", [])
    projects = roster.get("projects", [])

    # 1. Series inheritance: recurring meetings follow their series.
    if series_assignment and (
        series_assignment.get("client_id") or series_assignment.get("project_id")
    ):
        return Assignment(
            client_id=series_assignment.get("client_id"),
            project_id=series_assignment.get("project_id"),
            confidence=0.9,
            method="series",
        )

    # 2. Attendee email domain → client domain.
    domains = set()
    for attendee in attendees or []:
        email = (attendee.get("email") or "").strip().lower()
        if "@" in email:
            domain = email.rsplit("@", 1)[1]
            if domain and domain not in _GENERIC_DOMAINS:
                domains.add(domain)
    if domains:
        for client in clients:
            client_domains = {d.lower() for d in client.get("email_domains", [])}
            if client_domains & domains:
                # A title-matched project of this client refines the pick.
                project_id = None
                for project in projects:
                    if project.get("client_id") == client["id"] and _title_matches(
                        calendar_title, project
                    ):
                        project_id = project["id"]
                        break
                return Assignment(
                    client_id=client["id"],
                    project_id=project_id,
                    confidence=0.85,
                    method="domain",
                )

    # 3. Name/alias appears in the calendar event title.
    if calendar_title:
        for project in projects:
            if _title_matches(calendar_title, project):
                return Assignment(
                    client_id=project.get("client_id"),
                    project_id=project["id"],
                    confidence=0.75,
                    method="alias",
                )
        for client in clients:
            if _title_matches(calendar_title, client):
                return Assignment(client_id=client["id"], confidence=0.75, method="alias")

    return None


def build_context_text(roster: dict, assignment: Assignment, max_chars: int = 1500) -> str | None:
    """Client/project descriptions for summariser prompt injection."""
    parts: list[str] = []
    if assignment.client_id:
        client = next(
            (c for c in roster.get("clients", []) if c["id"] == assignment.client_id), None
        )
        if client:
            text = f"Client: {client['name']}"
            if client.get("description"):
                text += f" — {client['description']}"
            parts.append(text)
    if assignment.project_id:
        project = next(
            (p for p in roster.get("projects", []) if p["id"] == assignment.project_id),
            None,
        )
        if project:
            text = f"Project: {project['name']}"
            if project.get("description"):
                text += f" — {project['description']}"
            parts.append(text)
    if not parts:
        return None
    combined = "\n".join(parts)
    return combined[:max_chars]


class LlmAssigner:
    """LLM pick for meetings the deterministic pass left unassigned."""

    def __init__(self, summarisation_config: SummarisationConfig, config: TaggingConfig):
        self._summariser = Summariser(summarisation_config)
        self._config = config

    def assign(
        self,
        roster: dict,
        *,
        title: str,
        summary_markdown: str,
        attendees: list[dict] | None = None,
    ) -> Assignment | None:
        clients = roster.get("clients", [])
        projects = roster.get("projects", [])
        if not clients and not projects:
            return None
        try:
            response = self._call_llm(roster, title, summary_markdown, attendees or [])
            return self._parse(response, clients, projects)
        except Exception as e:
            logger.warning("LLM assignment failed: %s", e)
            return None

    def _call_llm(self, roster: dict, title: str, summary: str, attendees: list[dict]) -> str:
        roster_lines = []
        for client in roster.get("clients", []):
            roster_lines.append(
                f'- client id={client["id"]} name="{client["name"]}"'
                + (f' description="{client["description"]}"' if client.get("description") else "")
            )
        for project in roster.get("projects", []):
            roster_lines.append(
                f'- project id={project["id"]} name="{project["name"]}"'
                + (f" client_id={project['client_id']}" if project.get("client_id") else "")
                + (f' description="{project["description"]}"' if project.get("description") else "")
            )
        attendee_names = ", ".join(a.get("name") or a.get("email", "") for a in attendees if a)
        fence = "=" * 40
        user_msg = (
            "Roster:\n" + "\n".join(roster_lines) + "\n\n"
            f"Meeting title: {title}\n"
            f"Attendees: {attendee_names or 'unknown'}\n\n"
            f"{fence} BEGIN MEETING SUMMARY {fence}\n"
            f"{summary}\n"
            f"{fence} END MEETING SUMMARY {fence}"
        )
        return self._summariser.chat(ASSIGNMENT_PROMPT, user_msg)

    def _parse(self, response: str, clients: list[dict], projects: list[dict]):
        if not response:
            return None
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None

        client_ids = {c["id"] for c in clients}
        project_by_id = {p["id"]: p for p in projects}

        client_id = data.get("client_id")
        project_id = data.get("project_id")
        if client_id not in client_ids:
            client_id = None
        project = project_by_id.get(project_id)
        if project is None:
            project_id = None
        elif project.get("client_id"):
            # The project's own client is authoritative — an LLM pick of a
            # different client alongside it would be a contradiction.
            client_id = project["client_id"]

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if (client_id is None and project_id is None) or (confidence < self._config.min_confidence):
            return None
        return Assignment(
            client_id=client_id,
            project_id=project_id,
            confidence=round(confidence, 3),
            method="llm",
        )
