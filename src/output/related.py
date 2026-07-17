"""Resolve ## Related wikilinks to notes that actually exist."""

from __future__ import annotations

from pathlib import Path


def resolve_related(
    *,
    series_meetings: list[dict],
    this_started_at: float,
    project_note_name: str,
    vault_base: str,
    client_folder: str,
) -> list[tuple[str, str]]:
    """Return ``(label, note_stem)`` links for existing related notes.

    - ``Previous``: the most recent series sibling that started earlier and
      whose ``markdown_path`` file still exists.
    - ``Project``: the ``10 Projects`` note matching *project_note_name*, when
      such a note exists anywhere under the vault.

    Only existing notes are linked; nothing is fabricated.
    """
    out: list[tuple[str, str]] = []

    earlier = [
        m
        for m in (series_meetings or [])
        if (m.get("started_at") or 0.0) < this_started_at and (m.get("markdown_path") or "")
    ]
    earlier.sort(key=lambda m: m.get("started_at") or 0.0, reverse=True)
    for m in earlier:
        path = Path(m["markdown_path"])
        if path.exists():
            out.append(("Previous", path.stem))
            break

    if project_note_name:
        matches = list(Path(vault_base).rglob(f"{project_note_name}.md"))
        if matches:
            out.append(("Project", project_note_name))

    return out
