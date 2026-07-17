"""Resolve a client/project to its curated vault folder and tag.

The vault uses curated short tag slugs (client/siemens, project/siemens-16)
that do not match slugify() of the full names, so the mapping is an explicit
config map, never derived. Unknown clients or projects are flagged so the
caller can surface a warning rather than fabricate a tag.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaxonomyResolution:
    client_folder: str
    client_tag: str
    project_tag: str
    unknown_client: bool
    unknown_project: bool


def _lookup_ci(mapping: dict, name: str):
    """Case-insensitive lookup; returns (value, found)."""
    if not name:
        return None, False
    for key, value in (mapping or {}).items():
        if str(key).strip().lower() == name.strip().lower():
            return value, True
    return None, False


def resolve_taxonomy(
    client_name: str,
    project_name: str,
    taxonomy: dict,
    *,
    fallback_folder: str = "Unsorted",
) -> TaxonomyResolution:
    clients = (taxonomy or {}).get("clients", {})
    projects = (taxonomy or {}).get("projects", {})

    client_entry, client_found = _lookup_ci(clients, client_name)
    folder = fallback_folder
    client_tag = ""
    if client_found and isinstance(client_entry, dict):
        folder = client_entry.get("folder") or fallback_folder
        client_tag = client_entry.get("tag") or ""

    project_entry, project_found = _lookup_ci(projects, project_name)
    project_tag = project_entry if (project_found and isinstance(project_entry, str)) else ""

    return TaxonomyResolution(
        client_folder=folder,
        client_tag=client_tag,
        project_tag=project_tag,
        unknown_client=bool(client_name) and not client_found,
        unknown_project=bool(project_name) and not project_found,
    )
