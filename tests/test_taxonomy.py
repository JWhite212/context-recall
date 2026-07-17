from src.output.taxonomy import resolve_taxonomy

TAX = {
    "clients": {
        "Siemens": {"folder": "Siemens", "tag": "client/siemens"},
        "QVCCS Internal": {"folder": "QVCCS Internal", "tag": "qvccs-internal"},
    },
    "projects": {"Siemens 16 Smart UK Infrastructure": "project/siemens-16"},
}


def test_known_client_and_project():
    r = resolve_taxonomy("Siemens", "Siemens 16 Smart UK Infrastructure", TAX)
    assert r.client_folder == "Siemens"
    assert r.client_tag == "client/siemens"
    assert r.project_tag == "project/siemens-16"
    assert not r.unknown_client and not r.unknown_project


def test_case_insensitive_client_match():
    assert resolve_taxonomy("siemens", "", TAX).client_tag == "client/siemens"


def test_unknown_client_falls_back_and_flags():
    r = resolve_taxonomy("Acme Corp", "", TAX)
    assert r.client_folder == "Unsorted"
    assert r.client_tag == ""
    assert r.unknown_client is True


def test_flat_client_tag_is_preserved():
    # QVCCS Internal maps to a flat tag, not client/*
    r = resolve_taxonomy("QVCCS Internal", "", TAX)
    assert r.client_folder == "QVCCS Internal" and r.client_tag == "qvccs-internal"


def test_unknown_project_flags_but_client_still_resolves():
    r = resolve_taxonomy("Siemens", "Mystery Project", TAX)
    assert r.client_tag == "client/siemens"
    assert r.project_tag == "" and r.unknown_project is True


def test_empty_names_resolve_to_unknown_without_error():
    r = resolve_taxonomy("", "", TAX)
    assert r.client_folder == "Unsorted" and r.client_tag == "" and r.project_tag == ""
    assert r.unknown_client is False and r.unknown_project is False  # empty != unknown


def test_missing_taxonomy_is_safe():
    r = resolve_taxonomy("Siemens", "X", {})
    assert r.client_folder == "Unsorted" and r.client_tag == "" and r.project_tag == ""
