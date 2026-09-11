"""Targeted single-file re-index: ``mempalace mine <file> --mode projects``.

Until now projects mode required a directory. Re-indexing one curated file —
a project's ``CLAUDE.md`` after a claim in it was refuted — meant re-mining the
whole tree, which holds the palace write lock for as long as the tree is big
(the ``2g`` corpus that surfaced this is 111 MB).

The incident (mempalace#451): palace search kept returning the *transcript*
copy of a claim that ``2g/CLAUDE.md`` had since refuted. The indexed copy of
the card was filed 2026-09-01; the claim landed in the file on 09-03 and the
REFUTED banner on 09-05, so the indexed chunk contained neither. Transcripts
are mined continuously by hooks; curated project docs are only mined by a
manual whole-directory ``mempalace mine``, which nobody runs on a 111 MB tree.

A single-file mine must REPLACE that file's existing drawers, not add a second
set beside them — the drawers for ``/home/jp/Projects/2g/CLAUDE.md`` are keyed
by that absolute path, and ``process_file`` deletes by ``source_file`` before
re-inserting. These tests pin that path down with a recording collection.

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_miner_single_file.py -q
"""

import os
from pathlib import Path

import pytest
import yaml

from mempalace.miner import (
    MAX_FILE_SIZE,
    mine,
    resolve_project_root,
    scan_single_file,
)
from mempalace.palace import NORMALIZE_VERSION


def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# resolve_project_root — a file has no directory argument to derive wing/room
# from, so the miner has to find the project it belongs to.
# ---------------------------------------------------------------------------


def test_project_root_is_the_nearest_git_ancestor(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    target = repo / "docs" / "notes" / "CLAUDE.md"
    write_file(target, "# notes\n")

    assert resolve_project_root(target) == repo.resolve()


def test_project_root_is_the_nearest_mempalace_yaml_ancestor(tmp_path):
    repo = tmp_path / "outer"
    (repo / ".git").mkdir(parents=True)
    inner = repo / "subproject"
    write_file(inner / "mempalace.yaml", "wing: inner\n")
    target = inner / "README.md"
    write_file(target, "# inner\n")

    # Nearest marker wins: the subproject's yaml, not the outer repo's .git.
    assert resolve_project_root(target) == inner.resolve()


def test_project_root_falls_back_to_the_files_own_directory(tmp_path):
    loose = tmp_path / "loose" / "stray.md"
    write_file(loose, "# stray\n")

    assert resolve_project_root(loose) == loose.parent.resolve()


# ---------------------------------------------------------------------------
# scan_single_file — the same per-file gates scan_project applies while
# walking, applied once, without the walk.
# ---------------------------------------------------------------------------


def test_scan_single_file_returns_the_absolute_path(tmp_path):
    target = tmp_path / "CLAUDE.md"
    write_file(target, "# doc\n")

    assert scan_single_file(target, tmp_path) == [target.resolve()]


def test_scan_single_file_respects_gitignore(tmp_path, capsys):
    write_file(tmp_path / ".gitignore", "secret.md\n")
    target = tmp_path / "secret.md"
    write_file(target, "# hidden\n")

    assert scan_single_file(target, tmp_path) == []
    assert "secret.md" in capsys.readouterr().err


def test_scan_single_file_gitignore_can_be_disabled(tmp_path):
    write_file(tmp_path / ".gitignore", "secret.md\n")
    target = tmp_path / "secret.md"
    write_file(target, "# hidden\n")

    assert scan_single_file(target, tmp_path, respect_gitignore=False) == [target.resolve()]


def test_scan_single_file_honours_a_nested_gitignore(tmp_path):
    target = tmp_path / "sub" / "secret.md"
    write_file(target, "# hidden\n")
    write_file(tmp_path / "sub" / ".gitignore", "secret.md\n")

    assert scan_single_file(target, tmp_path) == []


def test_scan_single_file_rejects_an_unreadable_extension(tmp_path, capsys):
    target = tmp_path / "photo.jpeg"
    target.write_bytes(b"\xff\xd8\xff\xe0not really text")

    assert scan_single_file(target, tmp_path) == []
    assert "photo.jpeg" in capsys.readouterr().err


def test_scan_single_file_extension_gate_yields_to_an_exact_include(tmp_path):
    target = tmp_path / "CHANGELOG"
    write_file(target, "# changelog\n")

    assert scan_single_file(target, tmp_path, include_ignored=["CHANGELOG"]) == [target.resolve()]


def test_scan_single_file_rejects_a_symlink(tmp_path, capsys):
    real = tmp_path / "real.md"
    write_file(real, "# real\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    assert scan_single_file(link, tmp_path) == []
    assert "symlink" in capsys.readouterr().err


def test_scan_single_file_rejects_an_oversized_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("mempalace.miner.MAX_FILE_SIZE", 10)
    target = tmp_path / "big.md"
    write_file(target, "x" * 64)

    assert scan_single_file(target, tmp_path) == []
    assert "big.md" in capsys.readouterr().err


def test_scan_single_file_rejects_a_missing_path(tmp_path, capsys):
    assert scan_single_file(tmp_path / "nope.md", tmp_path) == []
    assert "nope.md" in capsys.readouterr().err


def test_max_file_size_is_still_the_shared_ceiling():
    """Guard against the monkeypatch above masking a constant rename."""
    assert MAX_FILE_SIZE > 0


# ---------------------------------------------------------------------------
# mine(<file>) — the whole point: one file, replacing its own drawers.
# ---------------------------------------------------------------------------


class _RecordingCollection:
    """Collection stand-in that records writes and can pre-seed prior drawers.

    Mirrors ``tests/test_miner.py::_StubCollection`` but lets a test seed the
    metadata ``file_already_mined`` will read, so the unchanged-file skip is
    exercisable.
    """

    def __init__(self, existing_metadatas=None):
        self.existing_metadatas = list(existing_metadatas or [])
        self.upsert_calls = []
        self.delete_calls = []
        self.query_calls = []

    def get(self, **kwargs):
        offset = kwargs.get("offset", 0)
        if offset:
            return {"ids": [], "metadatas": [], "documents": []}
        metas = self.existing_metadatas
        return {
            "ids": [f"seed_{i}" for i in range(len(metas))],
            "metadatas": list(metas),
            "documents": [""] * len(metas),
        }

    def upsert(self, *, documents, ids, metadatas):
        self.upsert_calls.append(
            {"documents": list(documents), "ids": list(ids), "metadatas": list(metadatas)}
        )

    def delete(self, where=None, **_kwargs):
        self.delete_calls.append(where)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]}

    def count(self):
        return sum(len(call["ids"]) for call in self.upsert_calls)

    # Convenience for the assertions below.
    def written_metadatas(self):
        return [meta for call in self.upsert_calls for meta in call["metadatas"]]


@pytest.fixture
def single_file_project(tmp_path):
    """A tiny project whose CLAUDE.md is long enough to produce drawers."""
    root = tmp_path / "2g_fixture"
    (root / ".git").mkdir(parents=True)
    with open(root / "mempalace.yaml", "w") as handle:
        yaml.dump(
            {
                "wing": "fixture_wing",
                "rooms": [
                    {"name": "references", "description": "Reference documents"},
                    {"name": "general", "description": "All project files"},
                ],
            },
            handle,
        )
    target = root / "references" / "CLAUDE.md"
    write_file(target, "The handset refuses this network.\n" * 200)
    # A sibling that must NOT be touched by a single-file mine.
    write_file(root / "references" / "other.md", "Unrelated content.\n" * 200)
    return root, target


def _mine_one(target, root, collection, **kwargs):
    return mine(
        str(target),
        str(root / "palace"),
        collection=collection,
        closets_collection=_RecordingCollection(),
        **kwargs,
    )


def test_mine_single_file_writes_drawers_for_that_file_only(single_file_project):
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection)

    metas = collection.written_metadatas()
    assert metas, "a single-file mine must write at least one drawer"
    assert {meta["source_file"] for meta in metas} == {str(target.resolve())}, (
        "drawers must be keyed by the file's ABSOLUTE path — the existing "
        "drawers a re-mine has to replace are keyed that way"
    )


def test_mine_single_file_chunk_indices_are_a_dense_sequence(single_file_project):
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection)

    indices = [meta["chunk_index"] for meta in collection.written_metadatas()]
    assert indices, "no drawers written — the chunk sequence assertion below is vacuous"
    assert indices == list(range(len(indices)))


def test_mine_single_file_detects_the_room_from_the_project_root(single_file_project):
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection)

    rooms = {meta["room"] for meta in collection.written_metadatas()}
    assert rooms == {"references"}, (
        "room detection is relative to the project root, so a file under "
        "references/ lands in the references room — not the file's own dir"
    )


def test_mine_single_file_takes_the_wing_from_the_projects_config(single_file_project):
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection)

    wings = {meta["wing"] for meta in collection.written_metadatas()}
    assert wings == {"fixture_wing"}, (
        "wing comes from the project root's mempalace.yaml, never from the file's own name"
    )


def test_mine_single_file_replaces_existing_drawers_for_that_path(single_file_project):
    """The REPLACE path: delete by source_file, then re-add."""
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection)

    assert {"source_file": str(target.resolve())} in collection.delete_calls, (
        "a re-mine must purge the file's stale drawers by absolute source_file "
        "before inserting the fresh chunks; otherwise the old chunks survive "
        "as orphans and search keeps returning the pre-edit text"
    )


def test_mine_single_file_leaves_siblings_alone(single_file_project):
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection)

    touched = {call["source_file"] for call in collection.delete_calls if call}
    assert touched == {str(target.resolve())}
    sources = {meta["source_file"] for meta in collection.written_metadatas()}
    assert str((root / "references" / "other.md").resolve()) not in sources


def test_mine_single_file_skips_an_unchanged_file(single_file_project):
    """Re-queuing an unchanged file is a no-op — the mtime skip still applies."""
    root, target = single_file_project
    collection = _RecordingCollection(
        existing_metadatas=[
            {
                "source_file": str(target.resolve()),
                "source_mtime": os.path.getmtime(target),
                "normalize_version": NORMALIZE_VERSION,
            }
        ]
    )

    # Positive control: the SAME fixture with no seeded drawers DOES write.
    control = _RecordingCollection()
    _mine_one(target, root, control)
    assert control.upsert_calls, "control failed — the skip assertion below is vacuous"

    _mine_one(target, root, collection)

    assert collection.upsert_calls == [], "unchanged file must not be re-written"
    assert collection.delete_calls == [], "unchanged file must not be purged"


def test_mine_single_file_remines_after_the_file_changes(single_file_project):
    root, target = single_file_project
    collection = _RecordingCollection(
        existing_metadatas=[
            {
                "source_file": str(target.resolve()),
                "source_mtime": os.path.getmtime(target) - 500,
                "normalize_version": NORMALIZE_VERSION,
            }
        ]
    )

    _mine_one(target, root, collection)

    assert collection.upsert_calls, "a changed file must be re-mined"
    assert {"source_file": str(target.resolve())} in collection.delete_calls


def test_mine_single_file_dry_run_writes_nothing(single_file_project, capsys):
    root, target = single_file_project
    collection = _RecordingCollection()

    _mine_one(target, root, collection, dry_run=True)

    assert collection.upsert_calls == []
    assert collection.delete_calls == []
    assert "DRY RUN" in capsys.readouterr().out


def test_mine_single_file_outside_a_project_uses_its_own_directory(tmp_path):
    loose_dir = tmp_path / "loose"
    target = loose_dir / "stray.md"
    write_file(target, "Standalone note about the network.\n" * 200)
    collection = _RecordingCollection()

    mine(
        str(target),
        str(tmp_path / "palace"),
        collection=collection,
        closets_collection=_RecordingCollection(),
    )

    metas = collection.written_metadatas()
    assert metas
    assert {meta["source_file"] for meta in metas} == {str(target.resolve())}


def test_mine_still_accepts_a_directory(single_file_project):
    """Regression guard: the directory path must be untouched."""
    root, _target = single_file_project
    collection = _RecordingCollection()

    mine(
        str(root),
        str(root / "palace"),
        collection=collection,
        closets_collection=_RecordingCollection(),
    )

    sources = {meta["source_file"] for meta in collection.written_metadatas()}
    assert len(sources) >= 2, "a directory mine must still pick up every file"
