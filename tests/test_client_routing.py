from pathlib import Path

from src.output.markdown_writer import MarkdownWriter
from src.output.note_context import NoteContext
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _ctx(**kw):
    base = dict(
        recall_id="m1",
        title="Weekly Review",
        date="2026-07-15",
        time="10:03",
        started_at=1_752_570_180.0,
        duration_minutes=28,
        word_count=10,
        client_folder="Siemens",
        enriched=True,
    )
    base.update(kw)
    return NoteContext(**base)


def _cfg(tmp_path, **kw):
    base = dict(
        enabled=True,
        vault_path=str(tmp_path),
        route_by_client=True,
        filename_template="{date}_{slug}.md",
    )
    base.update(kw)
    return MarkdownConfig(**base)


def test_routes_into_client_folder(tmp_path):
    path = MarkdownWriter(_cfg(tmp_path)).write_note(_ctx())
    assert path.parent.name == "Siemens" and path.exists()


def test_unknown_client_routes_to_unsorted(tmp_path):
    path = MarkdownWriter(_cfg(tmp_path)).write_note(_ctx(client_folder="Unsorted"))
    assert path.parent.name == "Unsorted"


def test_route_disabled_writes_flat(tmp_path):
    path = MarkdownWriter(_cfg(tmp_path, route_by_client=False)).write_note(_ctx())
    assert path.parent == Path(tmp_path)


def test_pre_enrichment_note_not_routed(tmp_path):
    # A pre-enrichment (enriched=False) note stays flat; the re-render routes it.
    path = MarkdownWriter(_cfg(tmp_path)).write_note(_ctx(enriched=False))
    assert path.parent == Path(tmp_path)


def test_reroute_moves_existing_note_no_duplicate(tmp_path):
    w = MarkdownWriter(_cfg(tmp_path))
    first = w.write_note(_ctx(client_folder="Unsorted"))  # pass 1, unknown client
    second = w.write_note(_ctx(client_folder="Siemens"), reuse_path=first)  # re-render
    assert second.parent.name == "Siemens"
    assert not first.exists()  # moved, not duplicated
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1


def test_reuse_path_preserves_basename_on_title_change(tmp_path):
    w = MarkdownWriter(_cfg(tmp_path))
    first = w.write_note(_ctx(title="Weekly Review", client_folder="Siemens"))
    # Title changed but the re-render must rewrite the SAME file (basename).
    second = w.write_note(
        _ctx(title="Totally New Title", client_folder="Siemens"), reuse_path=first
    )
    assert second == first
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1


def test_reuse_path_is_per_call_not_shared_state(tmp_path):
    # A reuse_path passed to one call must not leak into the next call on the
    # same shared writer (concurrency-safety regression guard).
    w = MarkdownWriter(_cfg(tmp_path))
    first = w.write_note(_ctx(title="Meeting A", client_folder="Siemens"))
    w.write_note(_ctx(title="Meeting A", client_folder="NTT"), reuse_path=first)  # relocates A
    # A fresh write with NO reuse_path must compute its own path, not reuse A's.
    b = w.write_note(_ctx(title="Meeting B", client_folder="Armacell"))
    assert b.parent.name == "Armacell"
    assert "meeting-b" in b.name


def test_pre_enrichment_reuse_keeps_note_in_current_folder(tmp_path):
    # Reprocess: pass 1 is pre-enrichment (enriched=False) but must NOT drag an
    # already-client-foldered note back to the vault root.
    w = MarkdownWriter(_cfg(tmp_path))
    enriched = w.write_note(_ctx(client_folder="Siemens", enriched=True))
    assert enriched.parent.name == "Siemens"
    # Reprocess pass 1 reuses the path with enriched=False.
    again = w.write_note(_ctx(client_folder="Siemens", enriched=False), reuse_path=enriched)
    assert again == enriched  # stayed in Siemens/, rewritten in place
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1


def test_linked_transcript_writes_companion(tmp_path):
    seg = TranscriptSegment(start=0, end=2, text="Hi", speaker="Me")
    ctx = _ctx(
        transcript=Transcript(segments=[seg], language="en", duration_seconds=2.0),
        transcript_mode="linked",
    )
    path = MarkdownWriter(_cfg(tmp_path)).write_note(ctx)
    companions = list(path.parent.glob("*(transcript).md"))
    assert companions and "Hi" in companions[0].read_text(encoding="utf-8")
    assert f"[[{companions[0].stem}]]" in path.read_text(encoding="utf-8")
