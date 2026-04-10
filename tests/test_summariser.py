"""Tests for the Summariser and MeetingSummary classes."""

import logging
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from src.summariser import MAX_TRANSCRIPT_WORDS, MeetingSummary, Summariser
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import SummarisationConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transcript(word_count: int) -> Transcript:
    """Build a Transcript containing roughly *word_count* words."""
    words_per_segment = 50
    segments = []
    remaining = word_count
    t = 0.0
    while remaining > 0:
        n = min(remaining, words_per_segment)
        text = " ".join(f"word{i}" for i in range(n))
        segments.append(TranscriptSegment(start=t, end=t + 5.0, text=text))
        remaining -= n
        t += 5.0
    return Transcript(
        segments=segments,
        language="en",
        language_probability=0.99,
        duration_seconds=t,
    )


# ---------------------------------------------------------------------------
# TestMeetingSummary
# ---------------------------------------------------------------------------

class TestMeetingSummary:
    def test_from_markdown_extracts_title(self):
        md = "# My Title\n\nSome body text."
        summary = MeetingSummary.from_markdown(md)
        assert summary.title == "My Title"

    def test_from_markdown_untitled_fallback(self):
        md = "## Not a top-level heading\n\nBody text only."
        summary = MeetingSummary.from_markdown(md)
        assert summary.title == "Untitled Meeting"

    def test_from_markdown_extracts_tags(self):
        md = "# Title\n\n## Tags\nfoo, bar, baz\n"
        summary = MeetingSummary.from_markdown(md)
        assert summary.tags == ["foo", "bar", "baz"]

    def test_from_markdown_empty_tags_section(self):
        md = "# Title\n\n## Tags\n\n"
        summary = MeetingSummary.from_markdown(md)
        assert summary.tags == []

    def test_from_markdown_no_tags_section(self):
        md = "# Title\n\nNo tags heading here."
        summary = MeetingSummary.from_markdown(md)
        assert summary.tags == []


# ---------------------------------------------------------------------------
# TestPrepareTranscript
# ---------------------------------------------------------------------------

class TestPrepareTranscript:
    def _make_summariser(self) -> Summariser:
        config = SummarisationConfig(backend="ollama")
        return Summariser(config)

    def test_short_transcript_warning(self, caplog):
        summariser = self._make_summariser()
        transcript = _make_transcript(5)

        with caplog.at_level(logging.WARNING, logger="src.summariser"):
            text, count = summariser._prepare_transcript(transcript)

        assert count == 5
        assert any("very short" in msg for msg in caplog.messages)

    def test_long_transcript_truncated(self):
        summariser = self._make_summariser()
        word_count = MAX_TRANSCRIPT_WORDS + 10_000
        transcript = _make_transcript(word_count)

        text, count = summariser._prepare_transcript(transcript)

        assert count == word_count
        assert "words omitted from middle of transcript" in text
        # The truncated text should be shorter than the original.
        assert len(text.split()) < word_count

    def test_normal_transcript_unchanged(self):
        summariser = self._make_summariser()
        transcript = _make_transcript(100)

        text, count = summariser._prepare_transcript(transcript)

        assert count == 100
        assert "omitted" not in text


# ---------------------------------------------------------------------------
# TestSummariserOllama
# ---------------------------------------------------------------------------

class TestSummariserOllama:
    def test_validate_ollama_url_localhost_allowed(self):
        result = Summariser._validate_ollama_url("http://localhost:11434")
        assert result == "http://localhost:11434"

    def test_validate_ollama_url_remote_rejected(self):
        with pytest.raises(ValueError, match="must point to localhost"):
            Summariser._validate_ollama_url("http://evil.com:11434")

    def test_validate_ollama_url_invalid_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            Summariser._validate_ollama_url("ftp://localhost:11434")

    @patch("src.summariser.httpx.post")
    def test_summarise_ollama_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {
                "content": "# Test Meeting\n\n## Summary\nGreat meeting.\n\n## Tags\ntest, demo",
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        config = SummarisationConfig(backend="ollama")
        summariser = Summariser(config)
        transcript = _make_transcript(100)

        result = summariser.summarise(transcript)

        assert result.title == "Test Meeting"
        assert "test" in result.tags
        assert "demo" in result.tags
        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# TestSummariserClaude
# ---------------------------------------------------------------------------

class TestSummariserClaude:
    def test_claude_missing_api_key(self):
        config = SummarisationConfig(backend="claude", anthropic_api_key="")
        summariser = Summariser(config)

        with pytest.raises(ValueError, match="API key not set"):
            summariser._get_claude_client()

    @patch("src.summariser.anthropic.Anthropic")
    def test_claude_rate_limit_error(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com"))
        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            response=response,
            body=None,
            message="rate limited",
        )

        config = SummarisationConfig(backend="claude", anthropic_api_key="test-key")
        summariser = Summariser(config)

        with pytest.raises(anthropic.RateLimitError):
            summariser.summarise(_make_transcript(100))

    @patch("src.summariser.anthropic.Anthropic")
    def test_claude_auth_error(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com"))
        mock_client.messages.create.side_effect = anthropic.AuthenticationError(
            response=response,
            body=None,
            message="auth error",
        )

        config = SummarisationConfig(backend="claude", anthropic_api_key="test-key")
        summariser = Summariser(config)

        with pytest.raises(anthropic.AuthenticationError):
            summariser.summarise(_make_transcript(100))

    @patch("src.summariser.anthropic.Anthropic")
    def test_claude_empty_response_returns_placeholder(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = []  # Empty content list.
        mock_client.messages.create.return_value = mock_message

        config = SummarisationConfig(backend="claude", anthropic_api_key="test-key")
        summariser = Summariser(config)

        result = summariser.summarise(_make_transcript(100))

        assert result.title == "Summary Unavailable"

    def test_summarise_unknown_backend(self):
        config = SummarisationConfig(backend="gpt4")
        summariser = Summariser(config)

        with pytest.raises(ValueError, match="Unknown summarisation backend"):
            summariser.summarise(_make_transcript(100))
