"""`purge` / `sync` must resolve the backend before the local-dir precheck (#418).

Measured incident (MemPalace 3.9.0, `MEMPALACE_BACKEND=postgres`, pgvector +
AGE served by palace-daemon on another host):

* A Postgres palace directory holds only ``dsn.env`` / ``hallways.json`` /
  ``tunnels.json`` / ``locks/`` / ``wal/`` — there is no local database file
  by design (``PostgresBackend.detect()`` returns ``False`` unconditionally).
  Both commands checked for one *before* resolving the backend and refused
  with "No palace found at …", so there was no working bulk delete on a
  Postgres palace at all: only ``drawer delete <id>``, one at a time.
* ``purge --source-file <path> --yes`` run from a client machine printed
  "No drawers found matching source-file=…" and exited 0 while
  ``mempalace mined`` still listed 115 drawers for that exact path. A purge
  that matched nothing and a purge that succeeded were indistinguishable
  from both the output and the exit status.

Both halves are the same defect seen twice: the command never says which
store it actually looked in.
"""

import argparse
import os
from unittest.mock import MagicMock, patch

import pytest


# ── backend_stores_data_locally ────────────────────────────────────────


class TestBackendStoresDataLocally:
    """The predicate the prechecks are supposed to be gated on."""

    def test_local_backends_store_locally(self):
        from mempalace.palace import backend_stores_data_locally

        assert backend_stores_data_locally("chroma") is True
        assert backend_stores_data_locally("sqlite_exact") is True

    def test_server_backends_do_not(self):
        from mempalace.palace import backend_stores_data_locally

        assert backend_stores_data_locally("postgres") is False
        assert backend_stores_data_locally("pgvector") is False
        assert backend_stores_data_locally("qdrant") is False

    def test_name_is_normalized(self):
        from mempalace.palace import backend_stores_data_locally

        assert backend_stores_data_locally("  POSTGRES ") is False

    def test_unknown_backend_keeps_the_guard(self):
        """An unregistered name must not silently skip the local-dir check."""
        from mempalace.palace import backend_stores_data_locally

        assert backend_stores_data_locally("not-a-backend") is True

    def test_embedded_milvus_is_local_but_a_milvus_server_is_not(self):
        from mempalace.palace import backend_stores_data_locally

        with patch("mempalace.backends.milvus.milvus_uri_is_server", return_value=False):
            assert backend_stores_data_locally("milvus") is True
        with patch("mempalace.backends.milvus.milvus_uri_is_server", return_value=True):
            assert backend_stores_data_locally("milvus") is False


# ── shared fixtures ────────────────────────────────────────────────────


def _purge_args(**overrides):
    defaults = {
        "palace": None,
        "wing": None,
        "room": None,
        "source_file": None,
        "yes": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _sync_args(**overrides):
    defaults = {
        "palace": None,
        "dir": None,
        "root": [],
        "wing": None,
        "dry_run": True,
        "daemon": False,
        "background": False,
        "yes": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _postgres_palace_dir(tmp_path):
    """A Postgres palace directory: sidecars only, no database artifact."""
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "dsn.env").write_text("MEMPALACE_POSTGRES_DSN=postgresql://x/y\n")
    (palace / "hallways.json").write_text("{}")
    (palace / "locks").mkdir()
    return palace


def _fake_collection(ids):
    col = MagicMock()
    col.get.return_value = {"ids": list(ids)}
    col.count.return_value = 0
    return col


_REMOVABLE_DAEMON = {
    "scanned": 12,
    "kept": 2,
    "gitignored": 7,
    "missing": 3,
    "unresolved": 0,
    "no_source": 0,
    "out_of_scope": 0,
    "removed_drawers": 0,
    "removed_closets": 0,
    "dry_run": True,
    "by_source": {},
    "unresolved_by_source": {},
}

_SYNC_REPORT = {
    "scanned": 3,
    "kept": 2,
    "gitignored": 1,
    "missing": 0,
    "unresolved": 0,
    "no_source": 0,
    "out_of_scope": 0,
    "removed_drawers": 0,
    "removed_closets": 0,
    "dry_run": True,
    "by_source": {},
    "unresolved_by_source": {},
}


# ── purge: backend resolution precedes the local-dir precheck ──────────


class TestPurgeOnServerBackend:
    def test_postgres_palace_is_not_refused_for_a_missing_local_database(
        self, tmp_path, monkeypatch, capsys
    ):
        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1", "d2"])

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col) as opener,
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        out = capsys.readouterr().out
        assert "No palace found" not in out
        assert opener.call_args.kwargs["backend"] == "postgres"
        col.delete.assert_called_once_with(where={"wing": "w"})

    def test_postgres_palace_works_without_the_directory_at_all(
        self, tmp_path, monkeypatch, capsys
    ):
        """A client that never ran `init` still has a real palace to purge."""
        missing = tmp_path / "nonexistent"
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"])

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
        ):
            mempalace_cli_purge(_purge_args(source_file="/a/b.md", palace=str(missing)))

        out = capsys.readouterr().out
        assert "No palace found" not in out
        col.delete.assert_called_once_with(where={"source_file": "/a/b.md"})

    def test_local_chroma_palace_still_refuses_a_missing_database(self, tmp_path, capsys):
        missing = tmp_path / "nonexistent"
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="any", palace=str(missing)))
        assert exc.value.code == 2, "palace unavailable, per cli.py's contract (#485)"
        assert "No palace found" in capsys.readouterr().out

    def test_purge_routes_through_the_resolved_backend_not_hardcoded_chroma(
        self, tmp_path, monkeypatch
    ):
        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        col = _fake_collection(["d1"])
        chroma = MagicMock()

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
            patch("mempalace.backends.chroma.ChromaBackend", chroma),
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        chroma.assert_not_called()


# ── purge: a zero match must not look like a success ───────────────────


class TestPurgeZeroMatchIsLoud:
    def test_zero_match_exits_non_zero(self, tmp_path, capsys):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")
        col = _fake_collection([])

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(source_file="/notes/a.md", palace=str(palace)))

        assert exc.value.code == 1
        col.delete.assert_not_called()

    def test_zero_match_names_the_store_it_looked_in(self, tmp_path, capsys):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")
        col = _fake_collection([])

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
            pytest.raises(SystemExit),
        ):
            mempalace_cli_purge(_purge_args(source_file="/notes/a.md", palace=str(palace)))

        out = capsys.readouterr().out
        assert "No drawers" in out
        assert str(palace) in out
        assert "chroma" in out

    def test_success_also_names_the_store(self, tmp_path, capsys):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")
        col = _fake_collection(["d1", "d2"])

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        out = capsys.readouterr().out
        assert "Purged 2 drawers" in out
        assert str(palace) in out


class TestPurgeFailuresAreNotSuccesses:
    """A purge that could not run must not exit 0 either (#418)."""

    def _chroma_palace(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")
        return palace

    def test_unreachable_backend_exits_non_zero(self, tmp_path, capsys):
        palace = self._chroma_palace(tmp_path)
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch(
                "mempalace.palace.get_collection",
                side_effect=RuntimeError("connection refused"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        assert exc.value.code == 2
        assert "connection refused" in capsys.readouterr().out

    def test_query_failure_exits_non_zero(self, tmp_path):
        palace = self._chroma_palace(tmp_path)
        col = MagicMock()
        col.get.side_effect = RuntimeError("boom")
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        assert exc.value.code == 2

    def test_delete_failure_exits_non_zero(self, tmp_path):
        palace = self._chroma_palace(tmp_path)
        col = _fake_collection(["d1"])
        col.delete.side_effect = RuntimeError("write refused")
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.palace.get_collection", return_value=col),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        assert exc.value.code == 2

    def test_backend_mismatch_is_a_usage_error(self, tmp_path, capsys):
        from mempalace.palace import BackendMismatchError

        palace = self._chroma_palace(tmp_path)
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch(
                "mempalace.palace.resolve_backend_name",
                side_effect=BackendMismatchError("chroma artifacts, postgres selected"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        assert exc.value.code == 2
        assert "Backend mismatch" in capsys.readouterr().out

    def test_unknown_backend_is_a_usage_error(self, tmp_path, capsys):
        palace = self._chroma_palace(tmp_path)
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch(
                "mempalace.palace.resolve_backend_name",
                side_effect=KeyError("unknown backend 'nope'"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        assert exc.value.code == 2
        assert "Unknown backend" in capsys.readouterr().out


# ── purge: daemon-strict routes to the daemon, never to a local palace ─


class TestPurgeUnderDaemonStrict:
    def test_source_file_purge_is_routed_to_the_daemon(self, capsys):
        calls = MagicMock(
            side_effect=[
                {"success": True, "dry_run": True, "match_count": 115, "closet_match_count": 4},
                {"success": True, "dry_run": False, "deleted": 115, "closets_deleted": 4},
            ]
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("mempalace.palace.get_collection") as local,
        ):
            mempalace_cli_purge(_purge_args(source_file="/p/FINDINGS.md"))

        local.assert_not_called()
        assert calls.call_args_list[0][0] == (
            "mempalace_delete_by_source",
            {"source_file": "/p/FINDINGS.md", "dry_run": True},
        )
        assert calls.call_args_list[1][0] == (
            "mempalace_delete_by_source",
            {"source_file": "/p/FINDINGS.md", "dry_run": False},
        )
        out = capsys.readouterr().out
        assert "115" in out
        assert "http://familiar:8085" in out

    def test_zero_match_over_the_daemon_also_exits_non_zero(self, capsys):
        calls = MagicMock(
            return_value={"success": True, "dry_run": True, "match_count": 0},
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(source_file="/p/FINDINGS.md"))

        assert exc.value.code == 1
        # Only the dry-run probe ran; nothing was committed.
        assert calls.call_count == 1
        out = capsys.readouterr().out
        assert "http://familiar:8085" in out

    def test_wing_purge_is_refused_rather_than_silently_run_locally(self, capsys):
        """No daemon bulk-delete exists for wing/room — refuse, don't retarget."""
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool") as calls,
            patch("mempalace.palace.get_collection") as local,
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="memorypalace"))

        assert exc.value.code == 2
        calls.assert_not_called()
        local.assert_not_called()
        assert "--palace" in capsys.readouterr().err

    def test_explicit_palace_flag_opts_back_into_the_local_path(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")
        col = _fake_collection(["d1"])

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool") as calls,
            patch("mempalace.palace.get_collection", return_value=col),
        ):
            mempalace_cli_purge(_purge_args(wing="w", palace=str(palace)))

        calls.assert_not_called()
        col.delete.assert_called_once_with(where={"wing": "w"})

    def test_wing_combined_with_source_file_is_refused_not_widened(self, capsys):
        """The daemon's bulk delete has no wing filter — refuse, never widen.

        ``mempalace_delete_by_source`` matches ``source_file`` across every
        wing. Passing ``--wing W --source-file F`` to it deletes F everywhere
        while the receipt says "wing=W source-file=F": a NARROWING flag
        silently dropped on a destructive command, which is the same
        output-disagrees-with-reality defect this PR exists to fix, with a
        blast radius. 27 of 5,477 sampled production sources live in more
        than one wing, so the flag is doing real work when it is given.
        """
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool") as calls,
            patch("mempalace.palace.get_collection") as local,
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(wing="memorypalace", source_file="/p/F.md"))

        assert exc.value.code == 2
        calls.assert_not_called()
        local.assert_not_called()
        err = capsys.readouterr().err
        assert "--source-file" in err
        assert "--palace" in err

    def test_room_combined_with_source_file_is_refused(self, capsys):
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool") as calls,
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_purge(_purge_args(room="2026-09", source_file="/p/F.md"))

        assert exc.value.code == 2
        calls.assert_not_called()

    def test_source_file_alone_is_still_routed(self):
        """The refusal must not swallow the case the daemon CAN do."""
        calls = MagicMock(
            side_effect=[
                {"success": True, "dry_run": True, "match_count": 2},
                {"success": True, "dry_run": False, "deleted": 2},
            ]
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
        ):
            mempalace_cli_purge(_purge_args(source_file="/p/F.md"))

        assert calls.call_count == 2

    def test_daemon_confirmation_is_required_without_yes(self):
        calls = MagicMock(
            return_value={"success": True, "dry_run": True, "match_count": 9},
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("mempalace.migrate.confirm_destructive_action", return_value=False) as confirm,
        ):
            mempalace_cli_purge(_purge_args(source_file="/p/a.md", yes=False))

        confirm.assert_called_once()
        assert calls.call_count == 1  # aborted before the commit call


# ── sync: same precheck defect ─────────────────────────────────────────


class TestSyncOnServerBackend:
    def test_postgres_palace_is_not_refused_for_a_missing_local_database(
        self, tmp_path, monkeypatch, capsys
    ):
        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", return_value=dict(_SYNC_REPORT)) as synced,
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace)))

        out = capsys.readouterr().out
        assert "has no" not in out
        assert "No palace found" not in out
        synced.assert_called_once()

    def test_postgres_palace_works_without_the_directory_at_all(self, tmp_path, monkeypatch):
        missing = tmp_path / "nonexistent"
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", return_value=dict(_SYNC_REPORT)) as synced,
        ):
            mempalace_cli_sync(_sync_args(palace=str(missing)))

        synced.assert_called_once()

    def test_sync_reports_the_backend_it_resolved(self, tmp_path, monkeypatch, capsys):
        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", return_value=dict(_SYNC_REPORT)),
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace)))

        assert "postgres" in capsys.readouterr().out

    def test_local_chroma_palace_still_refuses_a_missing_directory(self, tmp_path, capsys):
        missing = tmp_path / "nonexistent"
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace") as synced,
        ):
            mempalace_cli_sync(_sync_args(palace=str(missing)))

        assert "No palace found" in capsys.readouterr().out
        synced.assert_not_called()

    def test_local_chroma_palace_still_refuses_an_empty_directory(self, tmp_path, capsys):
        palace = tmp_path / "palace"
        palace.mkdir()
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace") as synced,
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace)))

        assert "has no" in capsys.readouterr().out
        synced.assert_not_called()


class TestSyncApplyAsksFirst:
    """`sync --apply` deletes; it must say so before it does (#418 review).

    Before this PR, `--apply` on a Postgres palace was a guaranteed no-op —
    the directory precheck refused it. Making it work turns it into a live,
    unconfirmed bulk delete: the reviewer ran the real classifier over one
    wing's top 400 sources and got 2,619 drawers deleted unconditionally.
    `purge` has always confirmed; `sync` now does too, on both routes.
    """

    _REMOVABLE = {**_SYNC_REPORT, "gitignored": 7, "missing": 3}

    def test_local_apply_confirms_before_deleting(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        synced = MagicMock(return_value=dict(self._REMOVABLE))

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", synced),
            patch("mempalace.migrate.confirm_destructive_action", return_value=True) as confirm,
        ):
            mempalace_cli_sync(
                _sync_args(palace=str(palace), wing="demo", dry_run=False, yes=False)
            )

        confirm.assert_called_once()
        # A preview pass, then the real one.
        assert [c.kwargs["dry_run"] for c in synced.call_args_list] == [True, False]

    def test_local_apply_aborts_when_declined(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        synced = MagicMock(return_value=dict(self._REMOVABLE))

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", synced),
            patch("mempalace.migrate.confirm_destructive_action", return_value=False),
        ):
            mempalace_cli_sync(
                _sync_args(palace=str(palace), wing="demo", dry_run=False, yes=False)
            )

        # Only the preview ran; nothing was deleted.
        assert [c.kwargs["dry_run"] for c in synced.call_args_list] == [True]

    def test_yes_skips_the_prompt_and_the_extra_scan(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        synced = MagicMock(return_value=dict(self._REMOVABLE))

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", synced),
            patch("mempalace.migrate.confirm_destructive_action") as confirm,
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace), wing="demo", dry_run=False, yes=True))

        confirm.assert_not_called()
        assert [c.kwargs["dry_run"] for c in synced.call_args_list] == [False]

    def test_dry_run_never_prompts(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        synced = MagicMock(return_value=dict(_SYNC_REPORT))

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", synced),
            patch("mempalace.migrate.confirm_destructive_action") as confirm,
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace), wing="demo", dry_run=True, yes=False))

        confirm.assert_not_called()
        assert synced.call_count == 1

    def test_nothing_removable_does_not_prompt_or_apply(self, tmp_path):
        """A preview showing zero removals needs no confirmation and no run."""
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        synced = MagicMock(return_value=dict(_SYNC_REPORT, gitignored=0, missing=0))

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", synced),
            patch("mempalace.migrate.confirm_destructive_action") as confirm,
        ):
            mempalace_cli_sync(
                _sync_args(palace=str(palace), wing="demo", dry_run=False, yes=False)
            )

        confirm.assert_not_called()
        assert [c.kwargs["dry_run"] for c in synced.call_args_list] == [True]

    def test_unscoped_apply_exits_2_without_prompting(self, tmp_path, capsys):
        """The preview must not launder away the apply-scope rule.

        ``sync_palace`` refuses an unscoped apply so it cannot auto-prune
        every wing. The confirmation preview runs with ``dry_run=True``,
        which passes that guard — so the rule has to be evaluated before the
        preview, or an unscoped ``--apply`` reaches an interactive prompt
        instead of exiting 2. Caught by an existing test when this prompt
        was first added.
        """
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        synced = MagicMock(return_value=dict(_SYNC_REPORT))

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.sync.sync_palace", synced),
            patch("mempalace.migrate.confirm_destructive_action") as confirm,
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace), dry_run=False, yes=False))

        assert exc.value.code == 2
        confirm.assert_not_called()
        synced.assert_not_called()
        assert "wing" in capsys.readouterr().err

    def test_unscoped_apply_over_the_daemon_also_exits_2(self, capsys):
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool") as calls,
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_sync(_sync_args(dry_run=False, yes=False))

        assert exc.value.code == 2
        calls.assert_not_called()

    def test_daemon_apply_confirms_before_deleting(self):
        calls = MagicMock(
            side_effect=[
                {"success": True, **self._REMOVABLE},
                {"success": True, **self._REMOVABLE},
            ]
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("mempalace.migrate.confirm_destructive_action", return_value=True) as confirm,
        ):
            mempalace_cli_sync(_sync_args(wing="x", dry_run=False, yes=False))

        confirm.assert_called_once()
        assert [c[0][1]["apply"] for c in calls.call_args_list] == [False, True]

    def test_daemon_apply_aborts_when_declined(self):
        calls = MagicMock(return_value={"success": True, **_REMOVABLE_DAEMON})
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("mempalace.migrate.confirm_destructive_action", return_value=False),
        ):
            mempalace_cli_sync(_sync_args(wing="x", dry_run=False, yes=False))

        assert calls.call_count == 1
        assert calls.call_args[0][1]["apply"] is False


class TestSyncUnderDaemonStrict:
    def test_dry_run_previews_through_the_daemon(self, capsys):
        payload = {"success": True, **_SYNC_REPORT}
        calls = MagicMock(return_value=payload)
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("mempalace.sync.sync_palace") as local,
        ):
            mempalace_cli_sync(_sync_args(dir="/home/jp/Projects/x", wing="x"))

        local.assert_not_called()
        name, sent = calls.call_args[0]
        assert name == "mempalace_sync"
        assert sent == {"project_dir": "/home/jp/Projects/x", "wing": "x", "apply": False}
        out = capsys.readouterr().out
        assert "http://familiar:8085" in out
        assert "Scanned" in out

    def test_apply_is_forwarded(self):
        calls = MagicMock(return_value={"success": True, **_SYNC_REPORT})
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
        ):
            mempalace_cli_sync(_sync_args(wing="x", dry_run=False))

        assert calls.call_args[0][1]["apply"] is True

    def test_daemon_failure_exits_non_zero(self, capsys):
        calls = MagicMock(return_value={"success": False, "error": "no palace"})
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch("mempalace.cli._call_daemon_tool", calls),
            pytest.raises(SystemExit) as exc,
        ):
            mempalace_cli_sync(_sync_args(wing="x"))

        assert exc.value.code == 1
        assert "no palace" in capsys.readouterr().err

    def test_explicit_palace_flag_opts_back_into_the_local_path(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        # sync's local precheck runs the backend's own detect(), which
        # verifies the SQLite magic header rather than mere file presence.
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool") as calls,
            patch("mempalace.sync.sync_palace", return_value=dict(_SYNC_REPORT)) as synced,
        ):
            mempalace_cli_sync(_sync_args(palace=str(palace)))

        calls.assert_not_called()
        synced.assert_called_once()

    def test_local_job_queue_flag_still_wins(self):
        """`--daemon` is the local job queue and must keep its own route."""
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._submit_daemon_cli_job") as submit,
            patch("mempalace.cli._call_daemon_tool") as calls,
        ):
            mempalace_cli_sync(_sync_args(daemon=True, wing="x"))

        submit.assert_called_once()
        calls.assert_not_called()


# ── thin indirection so the tests read as CLI invocations ──────────────


def mempalace_cli_purge(args):
    from mempalace.cli import cmd_purge

    return cmd_purge(args)


def mempalace_cli_sync(args):
    from mempalace.cli import cmd_sync

    return cmd_sync(args)


@pytest.fixture(autouse=True)
def _isolate_palace_env(monkeypatch, tmp_path):
    """Keep the suite off the operator's real palace/daemon configuration."""
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("PALACE_DAEMON_URL", raising=False)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path / "default-palace"))
    yield
    os.environ.pop("MEMPALACE_BACKEND", None)
