from pathlib import Path

from src.utils.config import load_config

_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def test_example_config_markdown_fields():
    md = load_config(_EXAMPLE).markdown
    assert md.transcript_mode in {"foldout", "linked", "omit", "inline"}
    assert md.route_by_client is True
    assert md.emit_my_tasks is True
    assert md.owner_display_name == "Jamie White (QVCCS)"
    assert "Jamie" in md.owner_identities or "Me" in md.owner_identities


def test_example_config_taxonomy_seed():
    md = load_config(_EXAMPLE).markdown
    assert "clients" in md.client_taxonomy and "projects" in md.client_taxonomy
    assert md.client_taxonomy["clients"]["Siemens"]["tag"] == "client/siemens"
    assert md.client_taxonomy["clients"]["Siemens"]["folder"] == "Siemens"
    assert md.client_taxonomy["projects"]["Siemens 16 Smart UK Infrastructure"] == (
        "project/siemens-16"
    )
    # QVCCS Internal maps to a flat tag, not a client/* tag.
    assert md.client_taxonomy["clients"]["QVCCS Internal"]["tag"] == "qvccs-internal"
