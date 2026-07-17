from src.output.attendees import fold_attendees


def test_folds_owner_identities_to_display_name_and_dedupes():
    out = fold_attendees(
        ["Me", "jamiecs@live.co.uk", "Amelia Lawton"],
        owner_identities=["me", "jamie", "jamiecs@live.co.uk", "j65541761@gmail.com"],
        owner_display_name="Jamie White (QVCCS)",
    )
    assert out == ["Jamie White (QVCCS)", "Amelia Lawton"]


def test_leaves_unknown_labels_untouched():
    out = fold_attendees(
        ["SPEAKER_01", "Remote"],
        owner_identities=["me"],
        owner_display_name="Jamie White (QVCCS)",
    )
    assert out == ["SPEAKER_01", "Remote"]


def test_owner_first_when_present():
    out = fold_attendees(
        ["Amelia Lawton", "Jamie"],
        owner_identities=["jamie"],
        owner_display_name="Jamie White (QVCCS)",
    )
    assert out[0] == "Jamie White (QVCCS)"
    assert out == ["Jamie White (QVCCS)", "Amelia Lawton"]


def test_no_owner_no_display_name_injected():
    out = fold_attendees(
        ["Amelia Lawton", "Seb"],
        owner_identities=["me", "jamie"],
        owner_display_name="Jamie White (QVCCS)",
    )
    assert out == ["Amelia Lawton", "Seb"]


def test_dedupes_repeated_others_and_skips_blanks():
    out = fold_attendees(
        ["Amelia Lawton", "Amelia Lawton", "", "  "],
        owner_identities=[],
        owner_display_name="Jamie White (QVCCS)",
    )
    assert out == ["Amelia Lawton"]
