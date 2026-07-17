"""
Markdown output writer for Obsidian-compatible vaults.

Produces a single .md file per meeting containing YAML frontmatter
(for Obsidian Dataview queries), the AI-generated summary, and
optionally the full timestamped transcript.

File naming uses the configured template with date and a slug
derived from the meeting title.
"""

import logging
import os
import time
from pathlib import Path

import yaml as _yaml
from slugify import slugify

from src.output.note_context import NoteContext
from src.summariser import MeetingSummary
from src.transcriber import Transcript
from src.utils.config import MarkdownConfig

logger = logging.getLogger(__name__)


class _BlockListDumper(_yaml.Dumper):
    """YAML dumper that indents block sequences under their mapping key.

    PyYAML's default emits list items flush with the key; the vault's
    Circleback notes (and Obsidian's own convention) indent them two
    spaces, so match that for a consistent, tooling-safe frontmatter.
    """

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _rewrite_frontmatter_title(content: str, new_title: str) -> str:
    """Return *content* with its YAML frontmatter ``title`` set to *new_title*.

    Falls back to returning *content* unchanged if the frontmatter block is
    missing or malformed, rather than risk corrupting the note.
    """
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            block = content[4:end]
            try:
                fm = _yaml.safe_load(block) or {}
            except _yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict):
                fm["title"] = new_title
                new_block = _yaml.dump(
                    fm, default_flow_style=False, allow_unicode=True, sort_keys=False
                ).rstrip()
                return f"---\n{new_block}\n---{content[end + 4 :]}"
    return content


def render_insights_section(results: list[dict]) -> str:
    """Render insight results into a ``## Insights`` markdown section.

    Groups results by ``definition_name`` and renders each as a sub-heading
    followed by bullets of its ``content`` — both list-mode and structured
    results already carry a human-readable ``content`` string, so no
    per-field formatting is needed here.

    Returns ``""`` when *results* is empty so callers can skip appending an
    empty section.
    """
    if not results:
        return ""

    grouped: dict[str, list[dict]] = {}
    for result in results:
        name = result.get("definition_name") or "Insights"
        grouped.setdefault(name, []).append(result)

    lines = ["## Insights", ""]
    for definition_name, items in grouped.items():
        lines.append(f"### {definition_name}")
        lines.append("")
        for item in items:
            content = (item.get("content") or "").strip()
            if content:
                lines.append(f"- {content}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


class MarkdownWriter:
    """Writes meeting output to an Obsidian-compatible Markdown vault."""

    def __init__(self, config: MarkdownConfig):
        self._config = config
        # Set when a filesystem error occurs during write(); the orchestrator
        # reads this to emit a pipeline.warning so the UI can surface
        # "Markdown output skipped: <reason>" instead of failing silently.
        self.last_error: str | None = None
        # Set by reuse_path() so the next write_note() targets an exact
        # existing file (the enriched re-render rewrites the note the
        # pre-enrichment pass already created, keyed on markdown_path).
        self._reuse_path: Path | None = None

    def reuse_path(self, path: Path) -> None:
        """Force the next write_note() to target this exact path (re-render)."""
        self._reuse_path = Path(path)

    def write(
        self,
        summary: MeetingSummary,
        transcript: Transcript,
        started_at: float,
        duration_seconds: float,
    ) -> Path | None:
        """Backwards-compatible pre-enrichment write.

        Builds a minimal :class:`NoteContext` from the summary and delegates
        to :meth:`write_note`. Kept so existing callers (the pipeline's
        pre-enrichment pass and the tests) are unaffected while the enriched
        re-render drives the same writer through a fuller context.

        Returns the path to the created file, or ``None`` if a filesystem
        error prevented the write (in which case ``last_error`` is set).
        """
        ctx = self._context_from_summary(summary, transcript, started_at, duration_seconds)
        return self.write_note(ctx)

    def _context_from_summary(
        self,
        summary: MeetingSummary,
        transcript: Transcript,
        started_at: float,
        duration_seconds: float,
    ) -> NoteContext:
        """Build a pre-enrichment context that reproduces the legacy note."""
        date_str = time.strftime("%Y-%m-%d", time.localtime(started_at))
        time_str = time.strftime("%H:%M", time.localtime(started_at))
        return NoteContext(
            recall_id="",
            title=summary.title,
            date=date_str,
            time=time_str,
            started_at=started_at,
            duration_minutes=int(duration_seconds / 60),
            word_count=transcript.word_count,
            extra_tags=list(summary.tags),
            summary_markdown=summary.raw_markdown,
            transcript=transcript,
            transcript_mode="inline" if self._config.include_full_transcript else "omit",
            enriched=False,
        )

    def write_note(self, ctx: NoteContext) -> Path | None:
        """Write a note from a :class:`NoteContext`.

        Atomic (temp file + ``os.replace``). Frontmatter is built from the
        context and serialised as block-list YAML; the body is composed by
        :func:`src.output.note_assembler.assemble_body`.
        """
        self.last_error = None
        # Consume any reuse_path once: the enriched re-render targets the file
        # the pre-enrichment pass created, and may relocate it to a client
        # folder (previous != filepath) without duplicating it.
        previous = self._reuse_path
        self._reuse_path = None
        try:
            filepath = self._target_path(ctx, previous)
        except ValueError:
            raise
        except OSError as e:
            self.last_error = f"Could not prepare vault path: {e}"
            logger.error("Markdown write failed: %s", self.last_error)
            return None

        from src.output.note_assembler import assemble_body, render_transcript

        # Linked transcript: write the companion note first so the main note
        # can wikilink it. Best-effort; a companion failure never fails the note.
        transcript_link: str | None = None
        if ctx.transcript_mode == "linked" and ctx.transcript is not None:
            companion = filepath.with_name(f"{filepath.stem} (transcript){filepath.suffix}")
            rows = render_transcript(ctx.transcript, "inline")
            try:
                companion.write_text(f"# {ctx.title} (transcript)\n\n{rows}\n", encoding="utf-8")
                transcript_link = companion.stem
            except OSError as e:
                logger.warning("Could not write transcript companion note: %s", e)

        frontmatter_yaml = self._dump_frontmatter(self._build_frontmatter(ctx))
        body = assemble_body(ctx, transcript_link)
        content = f"---\n{frontmatter_yaml}\n---\n\n{body}"

        # Atomic write: stream to a sibling temp file then os.replace() in
        # place. Prevents partial files if the daemon is killed mid-write or
        # the disk fills up between bytes.
        tmp_path = filepath.with_name(filepath.name + ".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, filepath)
        except OSError as e:
            self.last_error = f"Could not write markdown file {filepath}: {e}"
            logger.error("Markdown write failed: %s", self.last_error)
            # Best-effort cleanup of the temp file; ignore secondary errors.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        # Re-render relocation: once the new file is safely written, remove the
        # note's old copy so a client-folder change moves rather than duplicates.
        if (
            previous is not None
            and Path(previous).resolve() != filepath
            and Path(previous).exists()
        ):
            try:
                Path(previous).unlink()
            except OSError as e:
                logger.warning(
                    "Re-render wrote %s but could not remove old note %s: %s",
                    filepath,
                    previous,
                    e,
                )

        logger.info("Markdown written: %s", filepath)
        return filepath

    def _build_frontmatter(self, ctx: NoteContext) -> dict:
        """Build the YAML frontmatter mapping for a note.

        A pre-enrichment note carries the legacy shape (title/date/time/
        duration/word_count/tags/type). Once ``ctx.enriched`` is set the
        parity fields (client, project, meeting_type, attendees, source,
        recall_id, enriched) are included so the note matches the vault's
        Circleback frontmatter.
        """
        fm: dict = {
            "title": ctx.title,
            "date": ctx.date,
            "time": ctx.time,
        }
        if ctx.enriched:
            fm["client"] = ctx.client_name
            fm["project"] = ctx.project_name
            fm["meeting_type"] = ctx.meeting_type
        fm["duration_minutes"] = ctx.duration_minutes
        fm["word_count"] = ctx.word_count
        if ctx.enriched:
            fm["attendees"] = list(ctx.attendees)
        fm["tags"] = ctx.all_tags
        if ctx.enriched:
            fm["source"] = "context-recall"
            fm["recall_id"] = ctx.recall_id
            fm["enriched"] = True
        else:
            fm["type"] = "meeting-note"
        return fm

    def _dump_frontmatter(self, fm: dict) -> str:
        """Serialise frontmatter as block-list YAML, preserving key order.

        Uses an indenting dumper so block sequences sit two spaces under
        their key (matching the vault's Circleback notes); a flow list or a
        quoted list string corrupts the user's vault tooling. ``time`` is
        auto-quoted by PyYAML so a value like ``10:03`` is not re-read as a
        sexagesimal integer.
        """
        return _yaml.dump(
            fm,
            Dumper=_BlockListDumper,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=1000,
        ).rstrip()

    def _target_path(self, ctx: NoteContext, previous: Path | None = None) -> Path:
        """Compute the note's file path.

        Enriched notes route into ``<vault>/<client_folder>/`` when
        ``route_by_client`` is on; pre-enrichment notes stay flat. On a
        re-render (*previous* given) the existing basename is preserved, so
        only the client folder may change (title to filename changes are the
        rename path's job).
        """
        vault_path = Path(self._config.vault_path)
        route = (
            getattr(self._config, "route_by_client", False)
            and ctx.enriched
            and bool(ctx.client_folder)
        )
        base = vault_path / ctx.client_folder if route else vault_path
        os.makedirs(base, exist_ok=True)
        if previous is not None:
            filename = Path(previous).name
        else:
            time_str = ctx.time.replace(":", "-")
            title_slug = slugify(ctx.title, max_length=60)
            filename = self._config.filename_template.format(
                date=ctx.date,
                time=time_str,
                slug=title_slug or "meeting",
            )
            # Sanitize filename to prevent directory traversal.
            filename = filename.replace("/", "_").replace("\\", "_").lstrip(".")
        filepath = (base / filename).resolve()
        if not filepath.is_relative_to(vault_path.resolve()):
            raise ValueError(f"Generated filename would escape the vault directory: {filename!r}")
        return filepath

    def rename_note(self, old_path: Path, new_title: str, started_at: float) -> Path | None:
        """Rename a written note to reflect a new title.

        Rewrites the YAML frontmatter ``title`` and renames the file to the
        new title's slug (same template + start time as ``write()``).
        Returns the new path, the (title-updated) old path when the
        computed filename is unchanged, or ``None`` on a filesystem error
        (``last_error`` is set). Raises ``ValueError`` if the target would
        escape the vault, matching ``write()``'s existing behaviour.
        """
        self.last_error = None
        old_path = Path(old_path)
        vault_path = Path(self._config.vault_path)
        try:
            content = old_path.read_text(encoding="utf-8")
        except OSError as e:
            self.last_error = f"Could not read note {old_path}: {e}"
            logger.error("Markdown rename failed: %s", self.last_error)
            return None

        # Recompute the target filename exactly like write().
        date_str = time.strftime("%Y-%m-%d", time.localtime(started_at))
        time_str = time.strftime("%H-%M", time.localtime(started_at))
        title_slug = slugify(new_title, max_length=60)
        filename = self._config.filename_template.format(
            date=date_str, time=time_str, slug=title_slug or "meeting"
        )
        filename = filename.replace("/", "_").replace("\\", "_").lstrip(".")
        # Rename within the note's current folder, so an enriched note that
        # was routed into a client subfolder is not yanked back to the vault
        # root by a title change.
        new_path = (old_path.parent / filename).resolve()
        if not new_path.is_relative_to(vault_path.resolve()):
            raise ValueError(f"Rename target would escape the vault directory: {filename!r}")

        new_content = _rewrite_frontmatter_title(content, new_title)

        # Same computed filename: just rewrite the frontmatter in place.
        # Atomic write (tmp + os.replace), same crash-safe pattern as write(),
        # so a crash mid-write can't truncate the only copy of the note.
        if new_path == old_path.resolve():
            tmp_path = old_path.with_name(old_path.name + ".tmp")
            try:
                tmp_path.write_text(new_content, encoding="utf-8")
                os.replace(tmp_path, old_path)
            except OSError as e:
                self.last_error = f"Could not rewrite note {old_path}: {e}"
                logger.error("Markdown rename failed: %s", self.last_error)
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            return old_path

        # Different filename: avoid clobbering an unrelated existing note.
        if new_path.exists():
            stem, suffix = new_path.stem, new_path.suffix
            n = 2
            while True:
                candidate = new_path.with_name(f"{stem} ({n}){suffix}")
                if not candidate.exists():
                    new_path = candidate
                    break
                n += 1

        tmp_path = new_path.with_name(new_path.name + ".tmp")
        try:
            tmp_path.write_text(new_content, encoding="utf-8")
            os.replace(tmp_path, new_path)
        except OSError as e:
            self.last_error = f"Could not write renamed note {new_path}: {e}"
            logger.error("Markdown rename failed: %s", self.last_error)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        # The rename itself succeeded once the new file is written. Removing
        # the old file is best-effort: if it fails, a stale duplicate is far
        # better than reporting a false failure and orphaning the new file
        # with no path back to it, so this does NOT set last_error or
        # return None.
        try:
            old_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Renamed note written but could not remove old file %s: %s", old_path, e)

        logger.info("Markdown note renamed: %s -> %s", old_path, new_path)
        return new_path
