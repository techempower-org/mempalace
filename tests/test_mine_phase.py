"""A running mine reports its phase to the file its spawner names (#548)."""

import json
import os

import pytest

from mempalace import mine_phase
from mempalace.convo_miner import mine_convos


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(mine_phase, "_state", {"phase": None, "since": 0.0, "written": 0.0})


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_unset_env_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(mine_phase.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    mine_phase.report("open", files_total=3)
    assert list(tmp_path.iterdir()) == []


def test_report_writes_phase_counts_and_pid(tmp_path, monkeypatch):
    target = tmp_path / "phase.json"
    monkeypatch.setenv(mine_phase.ENV_VAR, str(target))
    mine_phase.report("file", file=2, files_total=5, chunks=40)
    rec = _read(target)
    assert rec["phase"] == "file"
    assert rec["pid"] == os.getpid()
    assert (rec["file"], rec["files_total"], rec["chunks"]) == (2, 5, 40)
    assert rec["since"] <= rec["updated"]
    assert [p.name for p in tmp_path.iterdir()] == ["phase.json"]


def test_same_phase_is_throttled_and_a_new_phase_always_writes(tmp_path, monkeypatch):
    target = tmp_path / "phase.json"
    monkeypatch.setenv(mine_phase.ENV_VAR, str(target))
    clock = [1000.0]
    monkeypatch.setattr(mine_phase.time, "time", lambda: clock[0])

    mine_phase.report("normalize", file=1)
    clock[0] += 0.5
    mine_phase.report("normalize", file=2)
    assert _read(target)["file"] == 1  # inside the interval: skipped

    clock[0] += 0.1
    mine_phase.report("chunk", file=2)
    rec = _read(target)
    assert (rec["phase"], rec["since"]) == ("chunk", 1000.6)  # phase change: written

    clock[0] += 5.0
    mine_phase.report("chunk", file=3)
    rec = _read(target)
    assert rec["file"] == 3
    assert rec["since"] == 1000.6  # same phase keeps its start
    assert rec["updated"] == 1005.6


def test_unwritable_path_never_raises(tmp_path, monkeypatch):
    target = tmp_path / "missing-dir" / "phase.json"
    monkeypatch.setenv(mine_phase.ENV_VAR, str(target))
    mine_phase.report("open")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_link_planted_at_the_tmp_name_is_not_written_through(tmp_path, monkeypatch):
    target = tmp_path / "phase.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (tmp_path / f"phase.json.{os.getpid()}.tmp").symlink_to(victim)
    monkeypatch.setenv(mine_phase.ENV_VAR, str(target))
    mine_phase.report("open")
    assert victim.read_text() == "keep me"
    assert not target.is_symlink()
    assert _read(target)["phase"] == "open"


def test_convo_mine_walks_its_phases_in_order(tmp_path, monkeypatch, capsys):
    convos = tmp_path / "convos"
    convos.mkdir()
    (convos / "chat.txt").write_text(
        "> What is memory?\nMemory is persistence.\n\n"
        "> Why does it matter?\nIt enables continuity.\n\n"
        "> How do we build it?\nWith structured storage.\n"
    )
    target = tmp_path / "phase.json"
    monkeypatch.setenv(mine_phase.ENV_VAR, str(target))

    seen = []
    real = mine_phase.report

    def spy(phase, **counts):
        seen.append(phase)
        real(phase, **counts)

    monkeypatch.setattr("mempalace.convo_miner.report_phase", spy)
    mine_convos(str(convos), str(tmp_path / "palace"), wing="test_phase")
    capsys.readouterr()

    assert seen == [
        "open",
        "prefetch",
        "normalize",
        "chunk",
        "file",
        "lock",
        "reconcile",
        "write",
        "hallways",
        "validate",
        "done",
    ]
    rec = _read(target)
    assert rec["phase"] == "done"
    assert rec["drawers_filed"] >= 2


def _write_convo(convos, exchanges):
    (convos / "chat.txt").write_text(
        "".join(f"> Question {i}?\nAnswer number {i} is persistence.\n\n" for i in exchanges)
    )


def test_a_lock_wait_reads_as_lock_with_the_file_counts(tmp_path, monkeypatch, capsys):
    """The phase is written BEFORE the blocking per-file flock (#548)."""
    import contextlib

    import mempalace.convo_miner as convo_miner

    convos = tmp_path / "convos"
    convos.mkdir()
    _write_convo(convos, range(3))
    target = tmp_path / "phase.json"
    monkeypatch.setenv(mine_phase.ENV_VAR, str(target))

    at_lock = []
    real_lock = convo_miner.mine_lock

    @contextlib.contextmanager
    def watching_lock(source_file):
        at_lock.append(_read(target))
        with real_lock(source_file):
            yield

    monkeypatch.setattr(convo_miner, "mine_lock", watching_lock)
    mine_convos(str(convos), str(tmp_path / "palace"), wing="test_phase")
    capsys.readouterr()

    assert len(at_lock) == 1
    rec = at_lock[0]
    assert rec["phase"] == "lock"
    assert (rec["file"], rec["files_total"]) == (1, 1)
    assert rec["chunks"] >= 2


def test_a_re_mine_of_a_shrunk_file_reports_its_purge(tmp_path, monkeypatch, capsys):
    convos = tmp_path / "convos"
    convos.mkdir()
    palace = str(tmp_path / "palace")
    _write_convo(convos, range(4))
    mine_convos(str(convos), palace, wing="test_phase")

    # Shrink the file and move its mtime, so the second pass re-mines it
    # with fewer chunks and leaves orphans to delete.
    _write_convo(convos, range(2))
    st = os.stat(convos / "chat.txt")
    os.utime(convos / "chat.txt", (st.st_atime, st.st_mtime + 10))

    seen = []
    real = mine_phase.report

    def spy(phase, **counts):
        seen.append((phase, counts))
        real(phase, **counts)

    monkeypatch.setattr("mempalace.convo_miner.report_phase", spy)
    monkeypatch.setenv(mine_phase.ENV_VAR, str(tmp_path / "phase.json"))
    mine_convos(str(convos), palace, wing="test_phase")
    capsys.readouterr()

    phases = [p for p, _ in seen]
    assert "refresh" in phases
    assert phases.index("lock") < phases.index("reconcile") < phases.index("refresh")
    assert phases.index("refresh") < phases.index("purge") < phases.index("hallways")
    purge = dict(seen)["purge"]
    assert purge["orphans"] >= 1
    assert purge["file"] == 1
