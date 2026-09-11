"""`mempalace doctor --curated` — curated docs modified after indexing (#451 E).

The palace keeps serving a pre-edit copy of a curated doc until it is
re-mined, and nothing tells the operator. This check answers, for the cwd
project's `CLAUDE.md` and `docs/**/*.md`, whether the file on disk has moved
on since the palace read it.

Three design constraints, each of which this file pins:

1. **Opt-in.** `mempalace doctor` runs in 405ms for all five existing
   checks (measured, two runs). The cheapest reliable source of recorded
   mtimes is one `mempalace_mined` call at 730ms — +180% on its own — so the
   check is behind `--curated` and the default budget is untouched.

2. **One staleness notion.** The comparison goes through
   `provenance.source_stale`, which already owns the mtime-vs-index
   comparison, the 60s grace window ("a file touched within a minute of its
   own mine is the mine, not an edit") and the honest `None` cases. This
   check must not grow a second one.

3. **Unknown is never clean.** A daemon predating the `max_source_mtime`
   field, a source with no recorded mtime, a file not on this machine — all
   report as *undecidable*, never as fresh. Measured on production: of six
   sampled curated-doc sources, TWO have no recorded mtime, so reading
   `None` as fresh would silently clear a third of them. Note that
   `prefetch_mined_set`'s docstring says to treat `None` as *stale* — right
   for the miner, where re-mining is cheap and safe, and wrong here, where
   it would fabricate "you edited this" out of missing bookkeeping.
"""

import argparse
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _doctor_args(**overrides):
    defaults = {"wing": None, "json": False, "curated": True, "palace": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _project(tmp_path, *, claude=True, docs=("a.md",), other=("notes.txt",)):
    root = tmp_path / "proj"
    root.mkdir()
    if claude:
        (root / "CLAUDE.md").write_text("# claude\n")
    if docs:
        (root / "docs").mkdir()
        for name in docs:
            target = root / "docs" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# doc\n")
    for name in other:
        (root / name).write_text("not curated\n")
    return root


def _checks_from(out: str) -> dict:
    return {c["check"]: c for c in json.loads(out)["checks"]}


# ── enumeration ────────────────────────────────────────────────────────


class TestCuratedDocsEnumeration:
    def test_finds_claude_md_and_docs_markdown_only(self, tmp_path):
        from mempalace.cli import _curated_doc_paths

        root = _project(tmp_path, docs=("a.md", "deep/b.md"), other=("notes.txt", "README.md"))
        found, _total = _curated_doc_paths(str(root))

        rel = sorted(os.path.relpath(p, root) for p in found)
        assert rel == [
            "CLAUDE.md",
            os.path.join("docs", "a.md"),
            os.path.join("docs", "deep", "b.md"),
        ]

    def test_paths_are_absolute_and_deterministic(self, tmp_path):
        from mempalace.cli import _curated_doc_paths

        root = _project(tmp_path, docs=("z.md", "a.md", "m.md"))
        first, _t1 = _curated_doc_paths(str(root))
        second, _t2 = _curated_doc_paths(str(root))

        assert first == second, "order must be stable across runs"
        assert first == sorted(first)
        assert all(os.path.isabs(p) for p in first)

    def test_is_capped_and_reports_the_pre_cap_total(self):
        """The cap must be visible to the caller, not silent.

        The first version returned only the capped list, so the truncated
        tail could never populate stale/never/unknown and the check printed
        ✓ "N up to date" exit 0 having never looked at most of the files.
        Measured in review: 2g has 795 curated docs, of which 745 were never
        examined — 6.3% coverage able to report clean. The old
        `test_is_capped` asserted exactly the length that hid it.
        """
        from mempalace.cli import _CURATED_DOCS_MAX, _curated_doc_paths

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _project(
                __import__("pathlib").Path(tmp),
                docs=tuple(f"d{i:03d}.md" for i in range(_CURATED_DOCS_MAX + 10)),
            )
            found, total = _curated_doc_paths(str(root))

        assert len(found) == _CURATED_DOCS_MAX
        assert total == _CURATED_DOCS_MAX + 11, "CLAUDE.md plus every docs/*.md"
        assert total > len(found)

    def test_claude_md_survives_the_cap_by_construction(self, tmp_path):
        """Not by ASCII luck: CLAUDE.md is placed first, then the docs capped."""
        from mempalace.cli import _CURATED_DOCS_MAX, _curated_doc_paths

        root = _project(tmp_path, docs=tuple(f"A{i:03d}.md" for i in range(_CURATED_DOCS_MAX + 5)))
        found, _total = _curated_doc_paths(str(root))

        assert found[0] == os.path.join(str(root), "CLAUDE.md")

    def test_missing_project_yields_nothing(self, tmp_path):
        from mempalace.cli import _curated_doc_paths

        assert _curated_doc_paths(str(tmp_path / "nope")) == ([], 0)


class TestDoctorOkIsTriState:
    """`ok` is True / None / False, and None must survive storage.

    `add()` coerced with `bool(ok)`, so every `warn` was stored as False —
    which made it render as ✗ (the glyph map's "warn" entry was unreachable)
    and flip the exit code. Both against the intent already in the code:
    `ok_all` tests `is not False` so that a None passes, and the closing
    message points at the ✗ lines only. Unnoticed because all five checks
    are ✓ on a healthy host, so no warn had ever been rendered. Fixed here
    because the curated check's "cannot tell" outcome is unexpressible
    otherwise.
    """

    def test_a_warn_renders_as_warn_and_does_not_fail_the_run(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path)
        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_doctor(_doctor_args(json=True, curated=False))

        checks = _checks_from(capsys.readouterr().out)
        daemon = checks["daemon"]
        assert daemon["ok"] is None, "a warn is 'cannot tell', not a failure"
        assert daemon["level"] == "warn"
        assert exc.value.code == 0, "a warn must not flip the exit code"

    def test_an_error_still_fails_the_run(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path)
        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("shutil.which", return_value=None),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_doctor(_doctor_args(json=True, curated=False))

        assert _checks_from(capsys.readouterr().out)["mcp_bridge"]["ok"] is False
        assert exc.value.code == 1

    def test_the_warn_glyph_is_reachable_in_the_text_path(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path)
        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=False, curated=False))

        out = capsys.readouterr().out
        assert "! daemon" in out, "warns rendered as ✗ while the ! glyph was dead code"


# ── the check, via the daemon ──────────────────────────────────────────


class TestCuratedCheckOverDaemon:
    def _run(self, root, mined_payload, *, args=None):
        from mempalace import cli

        calls = MagicMock(return_value=mined_payload)
        with (
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch(
                "mempalace.cli._call_daemon_rest", return_value={"total_drawers": 1, "wings": {}}
            ),
            patch("mempalace.cli._call_daemon_tool", calls),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(args or _doctor_args(json=True))
        return calls

    def test_a_file_modified_after_indexing_is_reported_stale(self, tmp_path, capsys):
        root = _project(tmp_path, docs=())
        claude = root / "CLAUDE.md"
        # Indexed an hour before the file's current mtime.
        indexed = os.path.getmtime(claude) - 3600
        payload = {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {
                            "source_file": str(claude),
                            "drawer_count": 3,
                            "max_source_mtime": indexed,
                        }
                    ]
                }
            }
        }
        self._run(root, payload)

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["ok"] is False
        assert "1 file(s) modified after indexing" in check["detail"]
        assert "mempalace mine" in check["detail"]

    def test_an_unchanged_file_is_clean(self, tmp_path, capsys):
        root = _project(tmp_path, docs=())
        claude = root / "CLAUDE.md"
        payload = {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {
                            "source_file": str(claude),
                            "drawer_count": 3,
                            "max_source_mtime": os.path.getmtime(claude),
                        }
                    ]
                }
            }
        }
        self._run(root, payload)

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["ok"] is True

    def test_a_source_with_no_recorded_mtime_is_undecidable_not_clean(self, tmp_path, capsys):
        root = _project(tmp_path, docs=())
        claude = root / "CLAUDE.md"
        payload = {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {"source_file": str(claude), "drawer_count": 3, "max_source_mtime": None}
                    ]
                }
            }
        }
        self._run(root, payload)

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["level"] == "warn"
        assert "cannot be checked" in check["detail"]
        assert "modified after indexing" not in check["detail"]

    def test_a_daemon_without_the_field_is_undecidable_for_every_file(self, tmp_path, capsys):
        """An older daemon omits max_source_mtime; that is not a clean bill."""
        root = _project(tmp_path, docs=("a.md",))
        payload = {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {"source_file": str(root / "CLAUDE.md"), "drawer_count": 3},
                        {"source_file": str(root / "docs" / "a.md"), "drawer_count": 1},
                    ]
                }
            }
        }
        self._run(root, payload)

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["level"] == "warn"
        assert "2" in check["detail"] and "cannot be checked" in check["detail"]

    def test_a_never_indexed_file_is_named_separately_from_a_stale_one(self, tmp_path, capsys):
        """Enumeration makes this reliable: absent from the source list really
        does mean never indexed, unlike 'absent from a search's top N'."""
        root = _project(tmp_path, docs=("a.md",))
        claude = root / "CLAUDE.md"
        payload = {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {
                            "source_file": str(claude),
                            "drawer_count": 3,
                            "max_source_mtime": os.path.getmtime(claude),
                        }
                    ]
                }
            }
        }
        self._run(root, payload)

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert "never indexed" in check["detail"]
        assert check["level"] == "warn"

    def test_one_daemon_call_per_doctor_run(self, tmp_path, capsys):
        root = _project(tmp_path, docs=tuple(f"d{i}.md" for i in range(8)))
        payload = {"sources_by_wing": {}}
        calls = self._run(root, payload)

        mined = [c for c in calls.call_args_list if c[0][0] == "mempalace_mined"]
        assert len(mined) == 1, "the whole point of max_source_mtime is one request"

    def test_a_daemon_error_is_undecidable_not_a_failure(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path, docs=())
        with (
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch(
                "mempalace.cli._call_daemon_rest",
                return_value={"total_drawers": 1, "wings": {}},
            ),
            patch(
                "mempalace.cli._call_daemon_tool",
                side_effect=cli.DaemonError("boom"),
            ),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["level"] == "warn"
        assert check["ok"] is not False, "a doctor never turns its own outage into a ✗"


class TestCuratedCheckNeverReportsCleanWhenItDidNotLook:
    """A truncated run can never be ✓ (#490 review).

    The cap is a budget guard, and a guard that hides what it skipped turns
    the check into the exact instrument this module refuses to be: one that
    answers "clean" about files it never examined. Measured live in review —
    2g 795 docs / 745 unexamined, realmwatch 148 / 98, memorypalace 72 / 22.
    """

    def _all_fresh_payload(self, paths):
        return {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {
                            "source_file": p,
                            "drawer_count": 1,
                            "max_source_mtime": os.path.getmtime(p),
                        }
                        for p in paths
                    ]
                }
            }
        }

    def test_truncation_downgrades_a_clean_verdict_to_warn(self, tmp_path, capsys):
        from mempalace import cli

        n_extra = 12
        root = _project(
            tmp_path,
            docs=tuple(f"d{i:03d}.md" for i in range(cli._CURATED_DOCS_MAX + n_extra - 1)),
        )
        (root / ".git").mkdir()
        examined, total = cli._curated_doc_paths(str(root))
        payload = self._all_fresh_payload(examined)

        with (
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch(
                "mempalace.cli._call_daemon_rest", return_value={"total_drawers": 1, "wings": {}}
            ),
            patch("mempalace.cli._call_daemon_tool", return_value=payload),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["ok"] is None, "every examined file was fresh, but most were not examined"
        assert check["level"] == "warn"
        assert f"{len(examined)} examined" in check["detail"]
        assert f"{total - len(examined)} not" in check["detail"]
        assert exc.value.code == 0, "not looking is not a failure — it is an unknown"

    def test_an_untruncated_clean_run_is_still_ok(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path, docs=("a.md",))
        (root / ".git").mkdir()
        examined, total = cli._curated_doc_paths(str(root))
        assert total == len(examined)
        payload = self._all_fresh_payload(examined)

        with (
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch(
                "mempalace.cli._call_daemon_rest", return_value={"total_drawers": 1, "wings": {}}
            ),
            patch("mempalace.cli._call_daemon_tool", return_value=payload),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["ok"] is True
        assert "not examined" not in check["detail"]
        assert exc.value.code == 0

    def test_a_stale_file_still_beats_truncation_in_the_verdict(self, tmp_path, capsys):
        """Truncation downgrades ✓ to !; it must not upgrade ✗ to !."""
        from mempalace import cli

        root = _project(
            tmp_path, docs=tuple(f"d{i:03d}.md" for i in range(cli._CURATED_DOCS_MAX + 5))
        )
        (root / ".git").mkdir()
        examined, _total = cli._curated_doc_paths(str(root))
        payload = self._all_fresh_payload(examined)
        # Make the first examined file stale.
        payload["sources_by_wing"]["proj"]["sources"][0]["max_source_mtime"] -= 3600

        with (
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch(
                "mempalace.cli._call_daemon_rest", return_value={"total_drawers": 1, "wings": {}}
            ),
            patch("mempalace.cli._call_daemon_tool", return_value=payload),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["ok"] is False
        assert "modified after indexing" in check["detail"]
        assert "not examined" in check["detail"], "the blind spot is still disclosed"
        assert exc.value.code == 1


class TestCuratedCheckNamesTheWorktreeCase:
    """From a linked worktree every curated doc reads "never indexed".

    True — the palace recorded the MAIN checkout's absolute paths — and
    useless as a signal. Found by running the real binary from the worktree
    the code was being written in: it printed "50 never indexed", which is
    both correct and alarming for no reason. An alarming line that means
    nothing is how a check earns a deselect (#454).
    """

    def _payload(self):
        return {"sources_by_wing": {"proj": {"sources": []}}}

    def test_the_note_appears_from_a_worktree(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path, docs=("a.md",))
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

        with (
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch(
                "mempalace.cli._call_daemon_rest", return_value={"total_drawers": 1, "wings": {}}
            ),
            patch("mempalace.cli._call_daemon_tool", return_value=self._payload()),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        detail = _checks_from(capsys.readouterr().out)["curated_docs"]["detail"]
        assert "linked worktree" in detail
        assert "main checkout" in detail

    def test_no_note_from_a_main_checkout(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path, docs=("a.md",))
        (root / ".git").mkdir()  # a main checkout: .git is a DIRECTORY

        with (
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch(
                "mempalace.cli._call_daemon_rest", return_value={"total_drawers": 1, "wings": {}}
            ),
            patch("mempalace.cli._call_daemon_tool", return_value=self._payload()),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        detail = _checks_from(capsys.readouterr().out)["curated_docs"]["detail"]
        assert "linked worktree" not in detail
        assert "never indexed" in detail


# ── the gate ───────────────────────────────────────────────────────────


class TestCuratedCheckIsOptIn:
    def test_absent_without_the_flag(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path)
        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("os.getcwd", return_value=str(root)),
            patch("mempalace.cli._call_daemon_tool") as called,
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True, curated=False))

        assert "curated_docs" not in _checks_from(capsys.readouterr().out)
        called.assert_not_called(), None

    def test_older_arg_namespaces_without_the_attr_do_not_crash(self, tmp_path, capsys):
        """`doctor` is called from tests and scripts with hand-built args."""
        from mempalace import cli

        root = _project(tmp_path)
        args = argparse.Namespace(wing=None, json=True)
        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(args)

        assert "curated_docs" not in _checks_from(capsys.readouterr().out)


# ── local / palace-host mode ───────────────────────────────────────────


class TestCuratedCheckLocalMode:
    def test_uses_prefetch_mined_set_when_there_is_no_daemon(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path, docs=())
        claude = root / "CLAUDE.md"
        prefetch = MagicMock(return_value={str(claude): os.path.getmtime(claude) - 3600})

        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("os.getcwd", return_value=str(root)),
            patch("mempalace.palace.get_collection", return_value=MagicMock()),
            patch("mempalace.palace.prefetch_mined_set", prefetch),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        prefetch.assert_called_once()
        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["ok"] is False
        assert "1 file(s) modified after indexing" in check["detail"]

    def test_an_unopenable_local_palace_is_undecidable(self, tmp_path, capsys):
        from mempalace import cli

        root = _project(tmp_path, docs=())
        with (
            patch("mempalace.cli._daemon_url", return_value=""),
            patch("os.getcwd", return_value=str(root)),
            patch(
                "mempalace.palace.get_collection",
                side_effect=RuntimeError("no palace"),
            ),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        check = _checks_from(capsys.readouterr().out)["curated_docs"]
        assert check["level"] == "warn"
        assert check["ok"] is not False


# ── the comparison is provenance's, not a second one ───────────────────


class TestCuratedCheckDelegatesStaleness:
    def test_it_calls_provenance_source_stale(self, tmp_path, capsys):
        """Pinning the seam: a second staleness notion is the thing to avoid."""
        from mempalace import cli

        root = _project(tmp_path, docs=())
        claude = root / "CLAUDE.md"
        stale = MagicMock(return_value=True)
        payload = {
            "sources_by_wing": {
                "proj": {
                    "sources": [
                        {
                            "source_file": str(claude),
                            "drawer_count": 1,
                            "max_source_mtime": 1.0,
                        }
                    ]
                }
            }
        }
        with (
            patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
            patch(
                "mempalace.cli._call_daemon_rest",
                return_value={"total_drawers": 1, "wings": {}},
            ),
            patch("mempalace.cli._call_daemon_tool", return_value=payload),
            patch("mempalace.provenance.source_stale", stale),
            patch("os.getcwd", return_value=str(root)),
            pytest.raises(SystemExit),
        ):
            cli.cmd_doctor(_doctor_args(json=True))

        stale.assert_called_once()
        hit = stale.call_args[0][0]
        assert hit["source_file"] == str(claude)
        assert "indexed_at" in hit, "provenance reads an ISO timestamp, not a float"
