"""
test_cli_search_output.py — enhanced ``mempalace search`` output (#191).

The second CLI slice for issue #191 adds three human-facing output
formats (``table`` default, ``compact`` one-liner, ``full`` no-truncation)
plus a ``--limit`` alias for ``--results``. ``--json`` keeps its existing
shape (backwards compat with #44 agent-shaped output).

These tests mock ``urllib.request.urlopen`` the same way
``test_cli_daemon.py`` does so the rendering logic is exercised without
touching a real daemon.
"""

import argparse
import json
from unittest.mock import patch

import pytest


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _envelope(payload: dict) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
    ).encode()


def _make_search_dispatcher(payload: dict):
    """Return a fake ``urlopen`` that returns ``payload`` for every call.

    ``GET`` requests (REST fast-path) get the bare payload; ``POST``
    requests (MCP tools/call) get the JSON-RPC envelope. Lets the same
    fixture serve ``/search/fast`` and ``mempalace_search`` fallback.
    """

    def fake_urlopen(req, timeout=None):
        if getattr(req, "data", None) is None:
            return _FakeResp(json.dumps(payload).encode())
        return _FakeResp(_envelope(payload))

    return fake_urlopen


def _hit(**overrides):
    """Default search-hit shape; override per-test."""
    base = {
        "wing": "projects",
        "room": "memorypalace",
        "source_file": "/path/to/notes.md",
        "similarity": 0.82,
        "text": "first line of drawer content\nsecond line\nthird line",
        "created_at": "2026-05-24T17:00:00Z",
    }
    base.update(overrides)
    return base


def _args(**overrides):
    """Argparse Namespace covering every flag ``cmd_search`` reads."""
    defaults = {
        "query": "graphql",
        "wing": None,
        "room": None,
        "results": 5,
        "limit": None,
        "format": None,
        "json": False,
        "quiet": False,
        "tags": None,
        "palace": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── format helpers ─────────────────────────────────────────────────────


class TestSearchFormatHelpers:
    def test_resolve_format_defaults_to_table(self):
        from mempalace import cli

        assert cli._resolve_search_format(_args()) == "table"

    def test_resolve_format_json_flag_legacy(self):
        from mempalace import cli

        assert cli._resolve_search_format(_args(json=True)) == "json"

    def test_resolve_format_explicit_wins_over_json_flag(self):
        from mempalace import cli

        # ``--format compact`` beats ``--json`` so power users can keep
        # the legacy flag in their shell history while overriding on
        # the command line.
        assert cli._resolve_search_format(_args(json=True, format="compact")) == "compact"

    def test_resolve_limit_defaults_to_results(self):
        from mempalace import cli

        assert cli._resolve_search_limit(_args(results=7)) == 7

    def test_resolve_limit_override_wins(self):
        from mempalace import cli

        assert cli._resolve_search_limit(_args(results=5, limit=20)) == 20


class TestRelevanceBar:
    def test_full_bar_at_one(self):
        from mempalace import cli

        bar = cli._relevance_bar(1.0, width=10)
        assert bar == "█" * 10

    def test_empty_bar_at_zero(self):
        from mempalace import cli

        bar = cli._relevance_bar(0.0, width=10)
        assert bar == "░" * 10

    def test_half_bar_at_half(self):
        from mempalace import cli

        bar = cli._relevance_bar(0.5, width=10)
        assert "█" in bar and "░" in bar
        assert len(bar) == 10

    def test_empty_string_when_similarity_missing(self):
        from mempalace import cli

        # BM25-only hits don't have a cosine score; the renderer falls
        # back to a numeric line so an empty string here is intentional.
        assert cli._relevance_bar(None) == ""

    def test_empty_string_on_invalid_similarity(self):
        from mempalace import cli

        assert cli._relevance_bar("not-a-number") == ""


class TestTruncateContent:
    def test_short_content_passes_through(self):
        from mempalace import cli

        shown, hidden = cli._truncate_content("one\ntwo\nthree", max_lines=12)
        assert shown == ["one", "two", "three"]
        assert hidden == 0

    def test_long_content_truncates_with_count(self):
        from mempalace import cli

        text = "\n".join(f"line {i}" for i in range(20))
        shown, hidden = cli._truncate_content(text, max_lines=5)
        assert len(shown) == 5
        assert hidden == 15

    def test_zero_max_lines_means_no_truncation(self):
        """``full`` format passes max_lines=0 to disable truncation."""
        from mempalace import cli

        text = "\n".join(f"line {i}" for i in range(20))
        shown, hidden = cli._truncate_content(text, max_lines=0)
        assert len(shown) == 20
        assert hidden == 0

    def test_empty_text(self):
        from mempalace import cli

        shown, hidden = cli._truncate_content("", max_lines=5)
        assert shown == []
        assert hidden == 0


class TestSearchUseColor:
    def test_quiet_disables_color(self):
        from mempalace import cli

        assert cli._search_use_color(quiet=True) is False

    def test_no_color_env_disables(self):
        from mempalace import cli

        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=False):
            assert cli._search_use_color(quiet=False) is False

    def test_non_tty_disables(self):
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with patch("sys.stdout.isatty", return_value=False):
                assert cli._search_use_color(quiet=False) is False


# ── format rendering through cmd_search ────────────────────────────────


class TestSearchTableFormat:
    """Default ``table`` format — multi-line per hit with metadata."""

    def test_renders_relevance_bar(self, capsys):
        from mempalace import cli

        payload = {"results": [_hit(similarity=0.91)], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args())

        out = capsys.readouterr().out
        assert "█" in out  # full-blocks from the bar
        assert "cosine=0.910" in out
        assert "first line of drawer content" in out

    def test_metadata_line_includes_source_and_timestamp(self, capsys):
        from mempalace import cli

        payload = {
            "results": [
                _hit(
                    source_file="/repo/docs/intro.md",
                    created_at="2026-05-24T17:30:00Z",
                )
            ],
            "warnings": [],
        }
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args())

        out = capsys.readouterr().out
        assert "/repo/docs/intro.md" in out
        assert "2026-05-24T17:30:00Z" in out

    def test_truncates_long_content_with_marker(self, capsys):
        from mempalace import cli

        long_text = "\n".join(f"line {i}" for i in range(40))
        payload = {"results": [_hit(text=long_text)], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args())

        out = capsys.readouterr().out
        # First N lines shown.
        assert "line 0" in out
        # Truncation marker appears.
        assert "more lines" in out
        # Tail lines are NOT shown.
        assert "line 39" not in out

    def test_renders_wing_and_room(self, capsys):
        from mempalace import cli

        payload = {
            "results": [_hit(wing="codequest", room="bugs")],
            "warnings": [],
        }
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args())

        out = capsys.readouterr().out
        assert "codequest" in out and "bugs" in out

    def test_includes_tags_when_present(self, capsys):
        from mempalace import cli

        payload = {
            "results": [_hit(tags=["python", "cli"])],
            "warnings": [],
        }
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args())

        out = capsys.readouterr().out
        assert "python" in out
        assert "cli" in out

    def test_bm25_only_hit_falls_back_to_numeric_line(self, capsys):
        """No cosine → no bar → numeric BM25 line so the score doesn't vanish."""
        from mempalace import cli

        payload = {
            "results": [_hit(similarity=None, bm25_score=4.21, matched_via="text")],
            "warnings": [],
        }
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args())

        out = capsys.readouterr().out
        assert "BM25" in out
        assert "4.21" in out


class TestSearchCompactFormat:
    """``--format compact`` — one line per hit."""

    def test_one_line_per_hit(self, capsys):
        from mempalace import cli

        # Distinct texts: two hits with the SAME words collapse to one line by
        # design (#526 PR 3); this test is about one line per hit.
        hits = [
            _hit(similarity=0.91),
            _hit(similarity=0.7, wing="other", text="a different drawer"),
        ]
        payload = {"results": hits, "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args(format="compact"))

        out = capsys.readouterr().out
        # Two hits → two "[N] " markers.
        assert "[1]" in out
        assert "[2]" in out
        # Both wings visible.
        assert "projects" in out
        assert "other" in out

    def test_preview_truncates_long_first_line(self, capsys):
        from mempalace import cli

        long_first = "x" * 200
        payload = {"results": [_hit(text=long_first)], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args(format="compact"))

        out = capsys.readouterr().out
        # Truncated preview ends with the ellipsis marker.
        assert "…" in out
        # Full 200-char string is not present unbroken.
        assert long_first not in out


class TestSearchFullFormat:
    """``--format full`` — table layout, no content truncation."""

    def test_full_does_not_truncate(self, capsys):
        from mempalace import cli

        long_text = "\n".join(f"line {i}" for i in range(40))
        payload = {"results": [_hit(text=long_text)], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args(format="full"))

        out = capsys.readouterr().out
        # Every line in the source must appear in the rendered output.
        assert "line 0" in out
        assert "line 39" in out
        # No truncation marker since nothing was truncated.
        assert "more lines" not in out


class TestSearchJsonBackwardsCompat:
    """``--json`` and ``--format json`` both keep the existing JSON shape."""

    def test_json_flag_emits_machine_readable_payload(self, capsys):
        from mempalace import cli

        payload = {"results": [_hit()], "warnings": [], "available_in_scope": 99}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_search(_args(json=True))

        # ``--json`` exits 0 when results exist, per #44 exit-code contract.
        assert ex.value.code == 0
        out = capsys.readouterr().out
        decoded = json.loads(out)
        assert decoded["results"][0]["wing"] == "projects"
        # ``query`` is normalised into the payload for shell pipelines.
        assert decoded["query"] == "graphql"

    def test_format_json_matches_legacy_flag(self, capsys):
        from mempalace import cli

        payload = {"results": [_hit()], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_search(_args(format="json"))

        assert ex.value.code == 0
        out = capsys.readouterr().out
        decoded = json.loads(out)
        assert decoded["results"][0]["similarity"] == 0.82

    def test_json_no_results_exits_one(self, capsys):
        from mempalace import cli

        payload = {"results": [], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_search(_args(json=True))

        # Empty results → exit 1 per the #44 contract; the JSON body is
        # still valid so shell scripts can parse and branch.
        assert ex.value.code == 1


class TestSearchLimit:
    """``--limit`` overrides ``--results`` — and is the value sent to the daemon."""

    def test_limit_overrides_results(self):
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            if getattr(req, "data", None) is None:
                # REST fast-path GET — capture ``limit`` from query string.
                from urllib.parse import urlparse, parse_qs

                qs = parse_qs(urlparse(req.full_url).query)
                captured["arguments"] = {"limit": int(qs["limit"][0])}
                return _FakeResp(json.dumps({"results": [], "warnings": []}).encode())
            captured["arguments"] = json.loads(req.data.decode())["params"]["arguments"]
            return _FakeResp(_envelope({"results": [], "warnings": []}))

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                # mode="fast" pins to BM25-only so the auto-with-fallback
                # path (techempower-org/mempalace#283) doesn't fire a
                # second POST that would defeat this test's intent —
                # checking that ``--limit`` lands in the request, not the
                # mode-selection logic.
                cli.cmd_search(_args(results=5, limit=12, mode="fast"))

        assert captured["arguments"]["limit"] == 12

    def test_results_used_when_limit_unset(self):
        from mempalace import cli

        captured = {}

        def fake_urlopen(req, timeout=None):
            if getattr(req, "data", None) is None:
                from urllib.parse import urlparse, parse_qs

                qs = parse_qs(urlparse(req.full_url).query)
                captured["arguments"] = {"limit": int(qs["limit"][0])}
                return _FakeResp(json.dumps({"results": [], "warnings": []}).encode())
            captured["arguments"] = json.loads(req.data.decode())["params"]["arguments"]
            return _FakeResp(_envelope({"results": [], "warnings": []}))

        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                # See sibling test above re: ``mode="fast"``.
                cli.cmd_search(_args(results=8, mode="fast"))

        assert captured["arguments"]["limit"] == 8


class TestSearchQuiet:
    """``--quiet`` suppresses the header chrome and ANSI colour."""

    def test_quiet_suppresses_header_borders(self, capsys):
        from mempalace import cli

        payload = {"results": [_hit()], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args(quiet=True))

        out = capsys.readouterr().out
        # The "=========" border is part of the header chrome — must
        # not appear under --quiet so piped output stays clean.
        assert "=" * 30 not in out
        # The hit itself still renders.
        assert "first line of drawer content" in out

    def test_no_color_under_quiet(self, capsys):
        from mempalace import cli

        payload = {"results": [_hit()], "warnings": []}
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with patch.dict("os.environ", env, clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_search_dispatcher(payload)):
                cli.cmd_search(_args(quiet=True))

        out = capsys.readouterr().out
        # ANSI escape codes start with \x1b[ — must not appear under quiet.
        assert "\x1b[" not in out


class TestSearchParserFlags:
    """Argparse wiring — both ``--format`` and ``--limit`` flow through.

    Mirrors ``TestParserAcceptsStats`` in ``test_cli_stats.py``: patch
    ``sys.argv`` and ``cmd_search`` to capture the parsed Namespace
    without dispatching to the daemon.
    """

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_search") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_format_compact_parses(self):
        ns = self._parse(["mempalace", "search", "q", "--format", "compact"])
        assert ns is not None
        assert ns.format == "compact"

    def test_limit_alias_parses(self):
        ns = self._parse(["mempalace", "search", "q", "--limit", "20"])
        assert ns is not None
        assert ns.limit == 20
        # Default ``--results`` is 15 (retrieval-breadth tuning, mempalace 3.3.7);
        # ``--limit`` is an alias that overrides it.
        assert ns.results == 15

    def test_default_format_is_none(self):
        """``cmd_search`` resolves None → ``table`` itself so explicit
        flag absence is distinguishable from ``--format table``."""
        ns = self._parse(["mempalace", "search", "q"])
        assert ns is not None
        assert ns.format is None
        assert ns.limit is None

    def test_invalid_format_rejected(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "search", "q", "--format", "yaml"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        # argparse exits with code 2 on choice violations.
        assert ex.value.code == 2
