"""`prune`, `status --json` and `compress` must resolve the backend first (#459).

#418 removed a local-directory precheck from ``purge`` and ``sync``: a
Postgres palace directory holds only sidecars (``dsn.env``,
``hallways.json``, ``tunnels.json``, ``locks/``, ``wal/``) and
``PostgresBackend.detect()`` returns ``False`` unconditionally, because the
drawers live in the service. Testing the directory for a database before
resolving the backend therefore refused every Postgres palace.

The same precheck survived in two more places, which is what this file is
about:

* ``palace._open_collection_or_explain`` — the shared "open it or explain
  why not" helper, whose State A (``not os.path.isdir``) and State B
  (``detect_backend_for_path() is None``) both run before the backend is
  opened. Callers: ``cli._emit_local_status_json``, ``cli.cmd_compress``,
  ``miner`` status, ``searcher``'s local fallback.
* ``cli.cmd_prune`` — its own copy of ``contains_palace_database`` (a
  chroma-only ``chroma.sqlite3`` test) plus a hardcoded ``ChromaBackend``.

Measured on a sidecar-only palace with ``MEMPALACE_BACKEND=postgres``, a DSN
pointing at a closed port and ``PALACE_DAEMON_STRICT=0``, i.e. where a
working command must fail with "connection refused":

    prune --stale-days 99999   No palace found at <dir>                exit 0
    --json status              {"error": "palace_unavailable", ...}     exit 2
    compress --dry-run         Palace dir ... has no backend database   exit 1

State B's own docstring gives the reason the guard exists — "some backends
lazily create their DB file on first open — calling the backend on this
state would silently mutate the filesystem for what should be a read-only
inspection". That is true of a *local* backend and vacuous for a
service-backed one, so the guard is gated on
``palace.backend_stores_data_locally`` rather than removed.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest


def _postgres_palace_dir(tmp_path):
    """A Postgres palace directory: sidecars only, no database artifact."""
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "dsn.env").write_text("MEMPALACE_POSTGRES_DSN=postgresql://x/y\n")
    (palace / "hallways.json").write_text("{}")
    (palace / "locks").mkdir()
    return palace


def _chroma_palace_dir(tmp_path):
    """A local chroma palace whose artifact passes the backend's own detect()."""
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
    return palace


def _fake_collection(ids=(), metadatas=None):
    col = MagicMock()
    col.count.return_value = len(ids)
    col.get.return_value = {"ids": list(ids), "metadatas": list(metadatas or [])}
    return col


# ── the shared seam ────────────────────────────────────────────────────


class TestOpenCollectionOrExplainResolvesBackendFirst:
    def test_server_backend_skips_the_directory_state_checks(self, tmp_path, monkeypatch):
        from mempalace.palace import _open_collection_or_explain

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"])
        opened = MagicMock(return_value=col)

        messages = []
        got = _open_collection_or_explain(str(palace), out=messages.append, opener=opened)

        assert got is col
        assert messages == [], f"a Postgres palace is not a broken palace: {messages}"
        assert opened.call_args.kwargs["backend"] == "postgres"

    def test_server_backend_works_with_no_directory_at_all(self, tmp_path, monkeypatch):
        from mempalace.palace import _open_collection_or_explain

        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"])
        messages = []

        got = _open_collection_or_explain(
            str(tmp_path / "nonexistent"),
            out=messages.append,
            opener=MagicMock(return_value=col),
        )

        assert got is col
        assert messages == []

    def test_local_backend_still_reports_a_missing_directory(self, tmp_path):
        from mempalace.palace import _open_collection_or_explain

        messages = []
        opened = MagicMock()

        got = _open_collection_or_explain(
            str(tmp_path / "nonexistent"), out=messages.append, opener=opened
        )

        assert got is None
        assert any("No palace found" in m for m in messages)
        opened.assert_not_called()

    def test_local_backend_still_reports_a_directory_with_no_database(self, tmp_path):
        """State B must survive: opening a local backend here would create files."""
        from mempalace.palace import _open_collection_or_explain

        palace = tmp_path / "palace"
        palace.mkdir()
        messages = []
        opened = MagicMock()

        got = _open_collection_or_explain(str(palace), out=messages.append, opener=opened)

        assert got is None
        assert any("has no" in m for m in messages)
        # State B exists so a read-only probe cannot create a DB. (Written as
        # a real assert: `opened.assert_not_called(), "msg"` is a tuple
        # expression whose message can never be reached.)
        assert not opened.called, "State B must not open the backend: it would create files"

    def test_local_backend_opens_a_healthy_palace(self, tmp_path):
        from mempalace.palace import _open_collection_or_explain

        palace = _chroma_palace_dir(tmp_path)
        col = _fake_collection(["d1"])
        messages = []

        got = _open_collection_or_explain(
            str(palace), out=messages.append, opener=MagicMock(return_value=col)
        )

        assert got is col
        assert messages == []


# ── prune ──────────────────────────────────────────────────────────────


def _prune_args(**overrides):
    defaults = {
        "palace": None,
        "stale_days": 90,
        "wing": None,
        "room": None,
        "confirm": False,
        "json": False,
        "quiet": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestPruneOnServerBackend:
    def test_postgres_palace_is_not_refused(self, tmp_path, monkeypatch, capsys):
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"], [{"filed_at": "2020-01-01T00:00:00+00:00"}])

        with patch("mempalace.palace.get_collection", return_value=col) as opener:
            cli.cmd_prune(_prune_args(palace=str(palace), stale_days=1))

        out = capsys.readouterr().out
        assert "No palace found" not in out
        assert opener.call_args.kwargs["backend"] == "postgres"

    def test_prune_uses_the_resolved_backend_not_hardcoded_chroma(self, tmp_path, monkeypatch):
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"], [{"filed_at": "2020-01-01T00:00:00+00:00"}])
        chroma = MagicMock()

        with (
            patch("mempalace.palace.get_collection", return_value=col),
            patch("mempalace.backends.chroma.ChromaBackend", chroma),
        ):
            cli.cmd_prune(_prune_args(palace=str(palace), stale_days=1))

        chroma.assert_not_called()

    def test_local_chroma_palace_still_refused_when_absent(self, tmp_path, capsys):
        from mempalace import cli

        # Exit 2 per cli.py's contract (#485); it used to return 0.
        with pytest.raises(SystemExit) as exc:
            cli.cmd_prune(_prune_args(palace=str(tmp_path / "nonexistent"), stale_days=1))
        assert exc.value.code == 2
        assert "No palace found" in capsys.readouterr().out

    def test_prune_names_the_target_it_scanned(self, tmp_path, capsys):
        from mempalace import cli

        palace = _chroma_palace_dir(tmp_path)
        col = _fake_collection(["d1"], [{"filed_at": "2020-01-01T00:00:00+00:00"}])

        with patch("mempalace.palace.get_collection", return_value=col):
            cli.cmd_prune(_prune_args(palace=str(palace), stale_days=1))

        out = capsys.readouterr().out
        assert str(palace) in out
        assert "chroma" in out

    def test_prune_json_reports_the_target(self, tmp_path, monkeypatch, capsys):
        """The SUCCESS payload names the target.

        This test used to omit the backend env, so it resolved chroma against
        a sidecar-only directory and asserted on the REFUSAL payload while
        claiming to check the success one. It passed for the wrong reason
        until #485 made that refusal exit 2.
        """
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"], [{"filed_at": "2020-01-01T00:00:00+00:00"}])

        with patch("mempalace.palace.get_collection", return_value=col):
            cli.cmd_prune(_prune_args(palace=str(palace), stale_days=1, json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["palace"] == str(palace)
        assert payload["backend"] == "postgres"

    def test_prune_json_error_path_also_names_the_target(self, tmp_path, capsys):
        """One refusal envelope for every command that has --json (#485)."""
        from mempalace import cli

        missing = tmp_path / "nonexistent"
        with pytest.raises(SystemExit) as exc:
            cli.cmd_prune(_prune_args(palace=str(missing), stale_days=1, json=True))

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "palace_unavailable"
        assert payload["palace_path"] == str(missing)
        assert "backend" in payload


# ── status --json and compress, through the shared seam ────────────────


def _mined_args(**overrides):
    defaults = {"palace": None, "wing": None, "limit": None, "json": False, "quiet": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestMinedOnServerBackend:
    """`mined` was the last cmd_* carrying its own copy of the precheck.

    Masked on the operator's host by its daemon-strict route, so the local
    path's refusal only surfaces with `--palace` or daemon-strict off — and
    it refused with exit 0, which is the shape #418 exists to eliminate.
    """

    def test_postgres_palace_is_not_refused(self, tmp_path, monkeypatch, capsys):
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = MagicMock()
        col.count.return_value = 1
        col.get.side_effect = [
            {"metadatas": [{"wing": "w", "source_file": "/a/b.md"}]},
            {"metadatas": []},
        ]

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col) as opener,
        ):
            cli.cmd_mined(_mined_args(palace=str(palace)))

        out = capsys.readouterr().out
        assert "No palace found" not in out
        assert opener.call_args.kwargs["backend"] == "postgres"

    def test_resolved_backend_is_used_not_hardcoded_chroma(self, tmp_path, monkeypatch):
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = MagicMock()
        col.count.return_value = 0
        col.get.return_value = {"metadatas": []}
        chroma = MagicMock()

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
            patch("mempalace.backends.chroma.ChromaBackend", chroma),
        ):
            cli.cmd_mined(_mined_args(palace=str(palace)))

        chroma.assert_not_called()

    def test_absent_local_palace_exits_non_zero(self, tmp_path, capsys):
        """The text path used to print and return 0; the JSON path exited 2.

        One condition must not hand a text caller and a JSON caller
        different exit codes.
        """
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_mined(_mined_args(palace=str(tmp_path / "nonexistent")))

        assert exc.value.code == 2
        assert "No palace found" in capsys.readouterr().out

    def test_absent_local_palace_json_still_exits_2(self, tmp_path, capsys):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_mined(_mined_args(palace=str(tmp_path / "nonexistent"), json=True))

        assert exc.value.code == 2
        assert "palace_unavailable" in capsys.readouterr().out

    def test_an_open_failure_says_why_instead_of_no_palace_found(
        self, tmp_path, monkeypatch, capsys
    ):
        """A refusal must not borrow the wrong message.

        An earlier draft of the extracted helper chose the text renderer by
        whether the backend was known, so a connection error — which knows
        its backend — printed "No palace found at <dir>". That is a refusal
        naming the wrong cause, which is the defect family #418/#459 are
        about. Found by running the CLI against a closed port, not by a
        unit test: the JSON payload carries the hint either way.
        """
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch(
                "mempalace.palace.get_collection",
                side_effect=RuntimeError("connection refused"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_mined(_mined_args(palace=str(palace)))

        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "connection refused" in out
        assert "No palace found" not in out

    def test_daemon_route_is_unaffected(self):
        from mempalace import cli

        calls = MagicMock(return_value={"sources_by_wing": {}, "total_sources": 0})
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("mempalace.palace.get_collection") as local,
        ):
            cli.cmd_mined(_mined_args())

        assert calls.call_args[0][0] == "mempalace_mined"
        local.assert_not_called()


class TestStatusJsonOnServerBackend:
    def test_postgres_palace_reports_drawers_not_palace_unavailable(
        self, tmp_path, monkeypatch, capsys
    ):
        from mempalace.cli import _emit_local_status_json

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = MagicMock()
        col.count.return_value = 2
        col.get.side_effect = [
            {"metadatas": [{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"}]},
            {"metadatas": []},
        ]

        with patch("mempalace.palace.get_collection", return_value=col):
            _emit_local_status_json(str(palace))

        payload = json.loads(capsys.readouterr().out)
        assert payload.get("error") != "palace_unavailable"
        assert payload["total_drawers"] == 2
        assert payload["palace_path"] == str(palace)

    def test_local_palace_without_a_database_still_reports_unavailable(self, tmp_path, capsys):
        from mempalace.cli import _emit_local_status_json

        palace = tmp_path / "palace"
        palace.mkdir()
        with pytest.raises(SystemExit) as exc:
            _emit_local_status_json(str(palace))
        assert exc.value.code == 2
        assert "palace_unavailable" in capsys.readouterr().out


class TestCompressOnServerBackend:
    def test_postgres_palace_is_not_refused(self, tmp_path, monkeypatch, capsys):
        from mempalace import cli

        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection([])

        args = argparse.Namespace(palace=str(palace), wing=None, dry_run=True, config=None)
        with patch("mempalace.palace.get_collection", return_value=col):
            cli.cmd_compress(args)

        assert "has no" not in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _isolate_backend_env(monkeypatch):
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("PALACE_DAEMON_URL", raising=False)
