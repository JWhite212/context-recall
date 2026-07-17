"""Resolve and fold attendee display names for the note."""

from __future__ import annotations


def fold_attendees(
    names: list[str],
    owner_identities: list[str],
    owner_display_name: str,
) -> list[str]:
    """Fold owner identities to the owner's display name, keep order, dedupe.

    A label matches an owner identity case-insensitively. Unknown labels are
    passed through unchanged (never guessed). The owner, when present, is
    listed first.
    """
    ident = {i.strip().lower() for i in owner_identities if i and i.strip()}
    owner_present = False
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        clean = (name or "").strip()
        if not clean:
            continue
        if clean.lower() in ident:
            owner_present = True
            continue
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    if owner_present:
        return [owner_display_name, *[n for n in out if n != owner_display_name]]
    return out
