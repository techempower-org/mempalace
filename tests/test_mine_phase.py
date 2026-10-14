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
        "hallways",
        "validate",
        "done",
    ]
    rec = _read(target)
    assert rec["phase"] == "done"
    assert rec["drawers_filed"] >= 2
