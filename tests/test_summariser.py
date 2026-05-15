"""Tests for the Summariser and MeetingSummary classes."""

import logging
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from src.summariser import (
    MAX_TRANSCRIPT_WORDS,
    MeetingSummary,
    Summariser,
    _with_retries,
)
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import SummarisationConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ollama_stream_mock(content: str) -> MagicMock:
    """Build a mock for ``httpx.stream`` that yields *content* as streaming JSON lines.

    Each call to the returned mock produces a fresh context-manager
    whose ``iter_lines()`` returns a new iterator, so the mock works
    correctly when ``_ollama_chat`` is called multiple times (e.g.
    during chunked summarisation).
    """
    import json

    lines = [
        json.dumps({"message": {"content": content}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True}),
    ]

    def _build_ctx(*_args, **_kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.iter_lines.return_value = iter(lines)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    mock_stream = MagicMock(side_effect=_build_ctx)
    return mock_stream


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

    def test_from_markdown_multiple_h1_takes_first(self):
        md = "# First Title\n\nSome content\n\n# Second Title\n\n## Tags\na, b"
        summary = MeetingSummary.from_markdown(md)
        assert summary.title == "First Title"

    def test_from_markdown_unicode_title(self):
        md = "# R\u00e9union de planification\n\n## Tags\nplanning"
        summary = MeetingSummary.from_markdown(md)
        assert summary.title == "R\u00e9union de planification"

    def test_from_markdown_raw_markdown_preserved(self):
        md = "# Title\n\n## Tags\nfoo, bar\n\nExtra content here."
        summary = MeetingSummary.from_markdown(md)
        assert summary.raw_markdown is md


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

    def test_summarise_ollama_success(self):
        mock_stream = _make_ollama_stream_mock(
            "# Test Meeting\n\n## Summary\nGreat meeting.\n\n## Tags\ntest, demo"
        )

        with patch("src.summariser.httpx.stream", mock_stream):
            config = SummarisationConfig(backend="ollama")
            summariser = Summariser(config)
            transcript = _make_transcript(100)

            result = summariser.summarise(transcript)

        assert result.title == "Test Meeting"
        assert "test" in result.tags
        assert "demo" in result.tags
        mock_stream.assert_called_once()


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


# ---------------------------------------------------------------------------
# TestSplitIntoChunks
# ---------------------------------------------------------------------------


class TestSplitIntoChunks:
    def test_short_text_returns_single_chunk(self):
        text = "hello world this is short"
        chunks = Summariser._split_into_chunks(text, target_words=100)
        assert chunks == [text]

    def test_splits_long_text_into_multiple_chunks(self):
        # 100 words, target 30 per chunk -> expect 3-4 chunks.
        text = " ".join(f"word{i}" for i in range(100))
        chunks = Summariser._split_into_chunks(text, target_words=30)
        assert len(chunks) >= 3
        # All words should be preserved across chunks.
        recombined = " ".join(chunks)
        assert recombined.split() == text.split()

    def test_empty_text_returns_single_chunk(self):
        chunks = Summariser._split_into_chunks("", target_words=100)
        assert chunks == [""]

    def test_exact_boundary_returns_single_chunk(self):
        text = " ".join(f"w{i}" for i in range(50))
        chunks = Summariser._split_into_chunks(text, target_words=50)
        assert chunks == [text]


# ---------------------------------------------------------------------------
# TestOllamaTimeout
# ---------------------------------------------------------------------------


class TestOllamaTimeout:
    def test_configurable_timeout_used(self):
        mock_stream = _make_ollama_stream_mock("# Title\n\n## Tags\nx")

        with patch("src.summariser.httpx.stream", mock_stream):
            config = SummarisationConfig(backend="ollama", ollama_timeout=900)
            summariser = Summariser(config)
            summariser.summarise(_make_transcript(100))

        _, kwargs = mock_stream.call_args
        assert kwargs["timeout"] == httpx.Timeout(
            connect=10.0,
            read=900.0,
            write=30.0,
            pool=10.0,
        )

    def test_timeout_error_message_includes_configured_value(self):
        mock_stream = MagicMock()
        mock_stream.side_effect = httpx.ReadTimeout("timed out")

        with (
            patch("src.summariser.httpx.stream", mock_stream),
            patch("src.summariser.time.sleep"),
        ):
            config = SummarisationConfig(backend="ollama", ollama_timeout=1200)
            summariser = Summariser(config)

            with pytest.raises(TimeoutError, match="1200s"):
                summariser.summarise(_make_transcript(100))


# ---------------------------------------------------------------------------
# TestChunkedOllama
# ---------------------------------------------------------------------------


class TestChunkedOllama:
    def test_large_transcript_triggers_chunking(self, caplog):
        """Transcripts exceeding chunk_threshold_words should be chunked."""
        mock_stream = _make_ollama_stream_mock("# Chunked Meeting\n\n## Tags\nchunk")

        config = SummarisationConfig(
            backend="ollama",
            chunk_threshold_words=8000,
        )
        summariser = Summariser(config)
        transcript = _make_transcript(9000)

        with patch("src.summariser.httpx.stream", mock_stream):
            with caplog.at_level(logging.INFO, logger="src.summariser"):
                result = summariser.summarise(transcript)

        assert result.title == "Chunked Meeting"
        # Multiple calls: one per chunk + one consolidation.
        assert mock_stream.call_count >= 3
        assert any("splitting into" in msg for msg in caplog.messages)
        assert any("Consolidating" in msg for msg in caplog.messages)

    def test_small_transcript_no_chunking(self):
        """Transcripts <= chunk_threshold_words should NOT be chunked."""
        mock_stream = _make_ollama_stream_mock("# Small Meeting\n\n## Tags\nsmall")

        config = SummarisationConfig(
            backend="ollama",
            chunk_threshold_words=8000,
        )
        summariser = Summariser(config)
        transcript = _make_transcript(5000)

        with patch("src.summariser.httpx.stream", mock_stream):
            result = summariser.summarise(transcript)

        assert result.title == "Small Meeting"
        mock_stream.assert_called_once()

    def test_configurable_chunk_threshold(self, caplog):
        """Custom chunk threshold is respected."""
        mock_stream = _make_ollama_stream_mock("# Threshold Meeting\n\n## Tags\nthreshold")

        # 6000 words with a 5000-word threshold should trigger chunking.
        config = SummarisationConfig(
            backend="ollama",
            chunk_threshold_words=5000,
        )
        summariser = Summariser(config)
        transcript = _make_transcript(6000)

        with patch("src.summariser.httpx.stream", mock_stream):
            with caplog.at_level(logging.INFO, logger="src.summariser"):
                summariser.summarise(transcript)
        assert mock_stream.call_count >= 2
        assert any("splitting into" in msg for msg in caplog.messages)

        # 4000 words with a 5000-word threshold should NOT trigger chunking.
        mock_stream.reset_mock()
        caplog.clear()
        transcript_small = _make_transcript(4000)
        with patch("src.summariser.httpx.stream", mock_stream):
            summariser.summarise(transcript_small)
        mock_stream.assert_called_once()


# ---------------------------------------------------------------------------
# TestChunkedClaude
# ---------------------------------------------------------------------------


class TestChunkedClaude:
    @patch("src.summariser.anthropic.Anthropic")
    def test_large_transcript_triggers_chunking(self, mock_anthropic_cls, caplog):
        """Transcripts exceeding chunk_threshold_words should be chunked for Claude."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="# Chunked Claude\n\n## Tags\nchunk")]
        mock_client.messages.create.return_value = mock_message

        config = SummarisationConfig(
            backend="claude",
            anthropic_api_key="test-key",
            chunk_threshold_words=8000,
        )
        summariser = Summariser(config)
        transcript = _make_transcript(9000)

        with caplog.at_level(logging.INFO, logger="src.summariser"):
            result = summariser.summarise(transcript)

        assert result.title == "Chunked Claude"
        # Multiple calls: one per chunk + one consolidation.
        assert mock_client.messages.create.call_count >= 3
        assert any("splitting into" in msg for msg in caplog.messages)

    @patch("src.summariser.anthropic.Anthropic")
    def test_small_transcript_no_chunking(self, mock_anthropic_cls):
        """Transcripts <= chunk_threshold_words should NOT be chunked for Claude."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="# Small Claude\n\n## Tags\nsmall")]
        mock_client.messages.create.return_value = mock_message

        config = SummarisationConfig(
            backend="claude",
            anthropic_api_key="test-key",
            chunk_threshold_words=8000,
        )
        summariser = Summariser(config)
        transcript = _make_transcript(5000)

        result = summariser.summarise(transcript)

        assert result.title == "Small Claude"
        mock_client.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# TestOllamaFallback
# ---------------------------------------------------------------------------


class TestOllamaFallback:
    """Test automatic fallback from Ollama to Claude on timeout."""

    def test_ollama_timeout_falls_back_to_claude(self):
        """When Ollama times out and Claude key is set, fall back to Claude."""
        config = SummarisationConfig(
            backend="ollama",
            anthropic_api_key="sk-test-key",
            ollama_model="test-model",
        )
        summariser = Summariser(config)
        transcript = Transcript(
            segments=[
                TranscriptSegment(start=0, end=5, text="Hello world test content"),
            ],
        )

        with patch.object(
            summariser,
            "_summarise_ollama",
            side_effect=TimeoutError("timeout"),
        ):
            with patch.object(summariser, "_summarise_claude") as mock_claude:
                mock_claude.return_value = MeetingSummary(
                    raw_markdown="# Fallback\n\n## Tags\ntest",
                    title="Fallback",
                    tags=["test"],
                )
                result = summariser.summarise(transcript)
                mock_claude.assert_called_once_with(transcript, None)
                assert result.title == "Fallback"

    def test_ollama_timeout_without_claude_raises(self):
        """When Ollama times out and no Claude key, re-raise TimeoutError."""
        config = SummarisationConfig(
            backend="ollama",
            anthropic_api_key="",
            ollama_model="test-model",
        )
        summariser = Summariser(config)
        transcript = Transcript(
            segments=[
                TranscriptSegment(start=0, end=5, text="Hello world"),
            ],
        )

        with patch.object(
            summariser,
            "_summarise_ollama",
            side_effect=TimeoutError("timeout"),
        ):
            with pytest.raises(TimeoutError):
                summariser.summarise(transcript)

    def test_ollama_success_no_fallback(self):
        """When Ollama succeeds, Claude is never called."""
        config = SummarisationConfig(
            backend="ollama",
            anthropic_api_key="sk-test-key",
            ollama_model="test-model",
        )
        summariser = Summariser(config)
        transcript = Transcript(
            segments=[
                TranscriptSegment(start=0, end=5, text="Hello world"),
            ],
        )

        with patch.object(summariser, "_summarise_ollama") as mock_ollama:
            mock_ollama.return_value = MeetingSummary(
                raw_markdown="# Success\n\n## Tags\nok",
                title="Success",
                tags=["ok"],
            )
            with patch.object(summariser, "_summarise_claude") as mock_claude:
                result = summariser.summarise(transcript)
                mock_ollama.assert_called_once()
                mock_claude.assert_not_called()
                assert result.title == "Success"


# TestOllamaNumCtx
# ---------------------------------------------------------------------------


class TestOllamaNumCtx:
    def test_num_ctx_in_payload(self):
        """Verify num_ctx is included in the Ollama API payload."""
        mock_stream = _make_ollama_stream_mock("# Title\n\n## Tags\nx")

        with patch("src.summariser.httpx.stream", mock_stream):
            config = SummarisationConfig(ollama_num_ctx=65536)
            summariser = Summariser(config)
            summariser.summarise(_make_transcript(100))

        _, kwargs = mock_stream.call_args
        payload = kwargs["json"]
        assert payload["options"]["num_ctx"] == 65536

    def test_default_num_ctx(self):
        """Verify default num_ctx is 32768."""
        config = SummarisationConfig()
        assert config.ollama_num_ctx == 32768


# ---------------------------------------------------------------------------
# TestWithRetries
# ---------------------------------------------------------------------------


class TestWithRetries:
    """Tests for the hand-rolled retry helper used by both backends."""

    def test_returns_first_success(self):
        call = MagicMock(return_value="ok")
        result = _with_retries(call, sleep=lambda _s: None)
        assert result == "ok"
        assert call.call_count == 1

    def test_retries_on_retryable_then_succeeds(self):
        call = MagicMock(
            side_effect=[
                httpx.ConnectError("boom"),
                "ok",
            ]
        )
        sleeps: list[float] = []
        result = _with_retries(call, max_attempts=3, backoff=2.0, sleep=sleeps.append)
        assert result == "ok"
        assert call.call_count == 2
        # First retry waits backoff * 2**0 == 2.0s.
        assert sleeps == [2.0]

    def test_exhausts_attempts_and_reraises(self):
        call = MagicMock(side_effect=httpx.ConnectError("boom"))
        sleeps: list[float] = []
        with pytest.raises(httpx.ConnectError):
            _with_retries(call, max_attempts=3, backoff=2.0, sleep=sleeps.append)
        assert call.call_count == 3
        # Two sleeps between three attempts: 2.0s then 4.0s.
        assert sleeps == [2.0, 4.0]

    def test_does_not_retry_on_non_retryable(self):
        call = MagicMock(side_effect=ValueError("fatal"))
        with pytest.raises(ValueError):
            _with_retries(call, sleep=lambda _s: None)
        assert call.call_count == 1

    def test_does_not_retry_on_auth_error(self):
        response = httpx.Response(401, request=httpx.Request("POST", "https://x"))
        err = anthropic.AuthenticationError(response=response, body=None, message="nope")
        call = MagicMock(side_effect=err)
        with pytest.raises(anthropic.AuthenticationError):
            _with_retries(call, sleep=lambda _s: None)
        assert call.call_count == 1

    def test_retries_on_rate_limit(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://x"))
        err = anthropic.RateLimitError(response=response, body=None, message="slow")
        call = MagicMock(side_effect=[err, "ok"])
        result = _with_retries(call, sleep=lambda _s: None)
        assert result == "ok"
        assert call.call_count == 2

    def test_retries_on_timeout_exception(self):
        call = MagicMock(side_effect=[httpx.ReadTimeout("slow"), "ok"])
        result = _with_retries(call, sleep=lambda _s: None)
        assert result == "ok"
        assert call.call_count == 2


# ---------------------------------------------------------------------------
# TestClaudeClientTimeout
# ---------------------------------------------------------------------------


class TestClaudeClientTimeout:
    @patch("src.summariser.anthropic.Anthropic")
    def test_claude_client_uses_explicit_per_stage_timeout(self, mock_anthropic_cls):
        config = SummarisationConfig(backend="claude", anthropic_api_key="sk-test")
        summariser = Summariser(config)
        summariser._get_claude_client()

        mock_anthropic_cls.assert_called_once()
        _, kwargs = mock_anthropic_cls.call_args
        assert kwargs["api_key"] == "sk-test"
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout == httpx.Timeout(
            connect=10.0,
            read=600.0,
            write=30.0,
            pool=10.0,
        )


# ---------------------------------------------------------------------------
# TestSummariserRetries
# ---------------------------------------------------------------------------


class TestSummariserRetries:
    """End-to-end retry behaviour through the public summarise() API."""

    @patch("src.summariser.time.sleep")
    @patch("src.summariser.anthropic.Anthropic")
    def test_claude_retries_then_succeeds(self, mock_anthropic_cls, _mock_sleep):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        request = httpx.Request("POST", "https://api.anthropic.com")
        good = MagicMock()
        good.content = [MagicMock(text="# Recovered\n\n## Tags\nx")]
        mock_client.messages.create.side_effect = [
            anthropic.APIConnectionError(request=request),
            good,
        ]

        config = SummarisationConfig(backend="claude", anthropic_api_key="sk-test")
        summariser = Summariser(config)
        result = summariser.summarise(_make_transcript(100))

        assert result.title == "Recovered"
        assert mock_client.messages.create.call_count == 2

    @patch("src.summariser.time.sleep")
    @patch("src.summariser.anthropic.Anthropic")
    def test_claude_auth_error_does_not_retry(self, mock_anthropic_cls, _mock_sleep):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        response = httpx.Response(401, request=httpx.Request("POST", "https://x"))
        mock_client.messages.create.side_effect = anthropic.AuthenticationError(
            response=response,
            body=None,
            message="auth error",
        )

        config = SummarisationConfig(backend="claude", anthropic_api_key="sk-test")
        summariser = Summariser(config)
        with pytest.raises(anthropic.AuthenticationError):
            summariser.summarise(_make_transcript(100))
        # Single attempt — no retries on auth.
        assert mock_client.messages.create.call_count == 1

    @patch("src.summariser.time.sleep")
    def test_ollama_retries_on_connect_then_succeeds(self, _mock_sleep):
        good_ctx = _make_ollama_stream_mock("# Recovered\n\n## Tags\nx")
        call_count = {"n": 0}

        def _stream(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.ConnectError("nope")
            return good_ctx(*args, **kwargs)

        with patch("src.summariser.httpx.stream", side_effect=_stream):
            config = SummarisationConfig(backend="ollama")
            summariser = Summariser(config)
            result = summariser.summarise(_make_transcript(100))

        assert result.title == "Recovered"
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# TestOllamaSSRF
# ---------------------------------------------------------------------------


class TestOllamaSSRF:
    """Hardened SSRF guard rejects non-local hosts, ports, and schemes."""

    def test_allows_default_port(self):
        result = Summariser._validate_ollama_url("http://localhost:11434")
        assert result == "http://localhost:11434"

    def test_allows_port_80(self):
        result = Summariser._validate_ollama_url("http://localhost:80")
        assert result == "http://localhost:80"

    def test_allows_port_443_https(self):
        result = Summariser._validate_ollama_url("https://localhost:443")
        assert result == "https://localhost:443"

    def test_allows_implicit_http_port(self):
        # Implicit port 80 for plain http should be accepted.
        result = Summariser._validate_ollama_url("http://localhost")
        assert result == "http://localhost"

    def test_allows_implicit_https_port(self):
        # Implicit port 443 for https should be accepted.
        result = Summariser._validate_ollama_url("https://localhost")
        assert result == "https://localhost"

    def test_rejects_disallowed_port(self):
        with pytest.raises(ValueError, match="port must be one of"):
            Summariser._validate_ollama_url("http://localhost:8080")

    def test_rejects_remote_host_even_on_allowed_port(self):
        with pytest.raises(ValueError, match="must point to localhost"):
            Summariser._validate_ollama_url("http://10.0.0.1:11434")

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="scheme must be http or https"):
            Summariser._validate_ollama_url("file:///etc/passwd")

    def test_constructor_rejects_invalid_url(self):
        config = SummarisationConfig(
            backend="ollama",
            ollama_base_url="http://evil.example.com:11434",
        )
        with pytest.raises(ValueError, match="must point to localhost"):
            Summariser(config)

    def test_loopback_ipv4(self):
        result = Summariser._validate_ollama_url("http://127.0.0.1:11434")
        assert result == "http://127.0.0.1:11434"
