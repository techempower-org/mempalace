"""`mempalace reconcile-docs` on the wire (#529).

The curated-docs hook is a listener, and a listener covers only the writes
that happen to reach it: measured on the live hook, a doc written in a lane
worktree, a doc landing by merge or `git pull`, a glob token mistaken for a
filename and a path a command merely mentioned all go unindexed. The verb
does not listen. It enumerates the filesystem, asks the palace what it
recorded, and queues a background single-file projects-mode mine for the
difference.

Two producer/consumer pairs live here and both are executed together, never
restated:

* the verb's ``/mine`` body and the body ``palace-doc-sync.sh`` sends
  (vendored below from the script's ``body=$(printf …)`` line; the palace host
  reads both with the same ``MineBody``) — asserted **byte for byte**;
* the verb's wing and the hook's ``basename | tr 'A-Z-' 'a-z_'`` — the real
  ``tr`` is run.

Everything drives the REAL CLI as a subprocess against a stub daemon that
records the raw bytes of every POST. A unit test that inspected a local
variable would have passed while ``mode: null`` went on the wire (#525); the
contract lives on the wire, so that is where it is tested.
"""

import json
import os
import pwd
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mempalace import cli

# Vendored from palace-doc-sync.sh (line `body=$(printf …)`), which runs on
# the palace host and is not installed here. Kept as a literal so the suite
# runs anywhere; where a live copy exists it is cross-checked below.
DOC_SYNC_PRINTF = '{"dir":"%s","wing":"%s","mode":"projects","background":true}'
LIVE_DOC_SYNC = os.path.join(
    pwd.getpwuid(os.getuid()).pw_dir, ".local", "bin", "palace-doc-sync.sh"
)

# Recorded mtimes must sit in the past: `source_stale` reads an index time in
# the future as a clock problem and answers None, not "current".
BASE = time.time() - 3600.0
GRACE = 60  # provenance._STALE_GRACE_SECONDS; a stale file is well past it


def _doc_sync_bytes(path, wing):
    """What palace-doc-sync.sh would put on the wire — by running its printf."""
    return subprocess.run(
        ["bash", "-c", 'printf \'%s\' "$1" "$2"' % DOC_SYNC_PRINTF, "_", path, wing],
        capture_output=True,
        check=True,
    ).stdout


# ── a stub daemon that records what it is sent ─────────────────────────


class _Daemon(BaseHTTPRequestHandler):
    #: source_file -> max_source_mtime (None is served as JSON null).
    mined: dict = {}
    #: raw bytes of every POST /mine, in order.
    posts: list = []
    #: status to answer POST /mine with.
    mine_status: int = 202

    def _reply(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path == "/mine":
            type(self).posts.append(raw)
            if type(self).mine_status >= 400:
                self._reply(type(self).mine_status, {"detail": "stub refuses"})
            else:
                self._reply(202, {"queued": True, "systemMessage": "queued (stub)"})
            return
        if self.path == "/mcp":
            req = json.loads(raw)
            params = req.get("params") or {}
            if params.get("name") != "mempalace_mined":
                self._reply(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req.get("id"),
                        "error": {"code": -32601, "message": "stub: unknown tool"},
                    },
                )
                return
            wing = (params.get("arguments") or {}).get("wing")
            sources = [
                {"source_file": path, "max_source_mtime": mtime}
                for path, mtime in type(self).mined.items()
            ]
            answer = {"sources_by_wing": {wing: {"sources": sources}} if sources else {}}
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {"content": [{"type": "text", "text": json.dumps(answer)}]},
                },
            )
            return
        self._reply(404, {"detail": "stub: no such route"})

    def do_GET(self):
        self._reply(200, {"ok": True})

    def log_message(self, *a):
        pass


@pytest.fixture
def daemon():
    _Daemon.mined = {}
    _Daemon.posts = []
    _Daemon.mine_status = 202
    srv = HTTPServer(("127.0.0.1", 0), _Daemon)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def scene(tmp_path):
    """A main-checkout project with four curated docs and a home to run from.

    Layout mirrors what the hook matches: ``<root>/CLAUDE.md`` and
    ``<root>/docs/**/*.md``. Every doc starts CURRENT (recorded == mtime); a
    test then mutates exactly one thing and asserts the mutation applied
    before it reads the verb's answer.
    """
    root = tmp_path / "Projects" / "proj-x"
    docs = {
        "CLAUDE.md": root / "CLAUDE.md",
        "docs/a.md": root / "docs" / "a.md",
        "docs/deep/b.md": root / "docs" / "deep" / "b.md",
        "docs/c.md": root / "docs" / "c.md",
    }
    for rel, path in docs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# %s\n" % rel)
        os.utime(path, (BASE, BASE))
    # A main checkout: `.git` is a DIRECTORY. The worktree test swaps it.
    (root / ".git").mkdir()
    _Daemon.mined = {str(p): BASE for p in docs.values()}
    home = tmp_path / "home"
    (home / ".mempalace").mkdir(parents=True)
    return root, docs, home


def _run(srv, home, argv, cwd=None):
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PALACE_DAEMON_URL": "http://127.0.0.1:%d" % srv.server_address[1],
            "PALACE_API_KEY": "x",
            "PALACE_DAEMON_STRICT": "1",
        }
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    return subprocess.run(
        [sys.executable, "-m", "mempalace.cli", "reconcile-docs"] + argv,
        cwd=cwd or root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _posted_dirs():
    return [json.loads(raw)["dir"] for raw in _Daemon.posts]


# ── 0. the instrument sees this tree ───────────────────────────────────


def test_the_subprocess_imports_the_tree_under_test():
    """An editable install resolves `mempalace` to whichever tree it was
    installed from — not necessarily the one these tests import. Pin it, or a
    green run here could be a run against someone else's code."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    out = subprocess.run(
        [sys.executable, "-c", "import mempalace; print(mempalace.__file__)"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert os.path.dirname(out) == os.path.dirname(os.path.abspath(cli.__file__))


# ── 1. the wire: one format, not two that drift ────────────────────────


class TestTheVerbSendsWhatPalaceDocSyncSends:
    def test_the_mine_body_is_byte_identical_to_the_scripts(self, daemon, scene):
        root, docs, home = scene
        never = str(docs["docs/a.md"])
        del _Daemon.mined[never]
        assert never not in _Daemon.mined

        proc = _run(daemon, home, [str(root)])

        assert proc.returncode == 0, proc.stderr[-400:]
        assert _Daemon.posts == [_doc_sync_bytes(never, "proj_x")], (
            "the verb and palace-doc-sync.sh must put the SAME bytes on the wire; "
            "got %r" % _Daemon.posts
        )

    def test_the_body_carries_projects_mode_and_background(self, daemon, scene):
        """Spelled out as fields too, so a failure names the field rather than
        an offset — and so a future change to the script's line shows up as
        two failures that disagree, not one that is silently re-vendored."""
        root, docs, home = scene
        del _Daemon.mined[str(docs["CLAUDE.md"])]
        _run(daemon, home, [str(root)])
        (body,) = [json.loads(raw) for raw in _Daemon.posts]
        assert body == {
            "dir": str(docs["CLAUDE.md"]),
            "wing": "proj_x",
            "mode": "projects",
            "background": True,
        }
        assert list(body) == ["dir", "wing", "mode", "background"]

    @pytest.mark.skipif(
        not os.path.isfile(LIVE_DOC_SYNC), reason="palace-doc-sync.sh runs on the palace host"
    )
    def test_the_vendored_printf_is_the_installed_one(self):
        text = open(LIVE_DOC_SYNC, encoding="utf-8").read()
        assert DOC_SYNC_PRINTF in text, "palace-doc-sync.sh changed its body; re-vendor"


class TestTheWingIsTheHooks:
    @pytest.mark.parametrize("name", ["memorypalace", "Realm-Watch", "2g", "palace-daemon"])
    def test_the_verb_derives_the_wing_the_hook_derives(self, name, tmp_path):
        hook = subprocess.run(
            ["bash", "-c", "basename \"$1\" | tr 'A-Z-' 'a-z_'", "_", str(tmp_path / name)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert cli._curated_wing_for(str(tmp_path / name)) == hook


# ── 2. what gets queued: each scene mutates one thing and proves it ────


class TestWhatGetsQueued:
    def test_a_stale_doc_is_queued(self, daemon, scene):
        root, docs, home = scene
        stale = docs["docs/deep/b.md"]
        os.utime(stale, (BASE + 2 * GRACE, BASE + 2 * GRACE))
        assert abs(os.stat(stale).st_mtime - (BASE + 2 * GRACE)) < 1, "utime did not apply"

        proc = _run(daemon, home, [str(root), "--json"])

        assert proc.returncode == 0, proc.stderr[-400:]
        assert _posted_dirs() == [str(stale)]
        report = json.loads(proc.stdout)
        assert (report["queued"], report["current"]) == (1, 3)

    def test_a_never_indexed_doc_is_queued(self, daemon, scene):
        root, docs, home = scene
        never = str(docs["docs/c.md"])
        del _Daemon.mined[never]
        assert never not in _Daemon.mined

        proc = _run(daemon, home, [str(root), "--json"])

        assert proc.returncode == 0, proc.stderr[-400:]
        assert _posted_dirs() == [never]
        assert json.loads(proc.stdout)["queued"] == 1

    def test_an_undecidable_doc_is_reported_and_never_queued(self, daemon, scene):
        """`max_source_mtime: null` is an absent answer, not a stale one.
        Folding it into stale would re-mine a wing on a missing field."""
        root, docs, home = scene
        unknown = str(docs["docs/a.md"])
        _Daemon.mined[unknown] = None
        assert _Daemon.mined[unknown] is None

        proc = _run(daemon, home, [str(root), "--json"])

        assert proc.returncode == 1, "ran and selected nothing → 1; got %d" % proc.returncode
        assert _Daemon.posts == []
        report = json.loads(proc.stdout)
        assert (report["undecidable"], report["queued"], report["current"]) == (1, 0, 3)

    def test_a_gitignored_doc_is_skipped_through_the_miners_own_matcher(self, daemon, scene):
        root, docs, home = scene
        ignored = docs["docs/c.md"]
        (root / ".gitignore").write_text("docs/c.md\n")
        assert cli._doc_is_gitignored(str(root), str(ignored)) is True, "gitignore did not apply"
        del _Daemon.mined[str(ignored)]  # would be "never indexed" if enumerated

        proc = _run(daemon, home, [str(root), "--json"])

        assert _Daemon.posts == [], "an ignored doc was queued: %r" % _posted_dirs()
        report = json.loads(proc.stdout)
        assert (report["skipped_ignored"], report["current"]) == (1, 3)
        assert proc.returncode == 1

    def test_all_current_queues_nothing_and_exits_1(self, daemon, scene):
        """The control: with no mutation the verb must find nothing to do."""
        root, docs, home = scene
        proc = _run(daemon, home, [str(root), "--json"])
        assert _Daemon.posts == []
        assert json.loads(proc.stdout)["current"] == 4
        assert proc.returncode == 1

    def test_the_posted_set_is_exactly_stale_plus_never(self, daemon, scene):
        """The invariant all the scenes above are instances of, in one run."""
        root, docs, home = scene
        stale, never, unknown = docs["CLAUDE.md"], docs["docs/a.md"], docs["docs/c.md"]
        os.utime(stale, (BASE + 2 * GRACE, BASE + 2 * GRACE))
        del _Daemon.mined[str(never)]
        _Daemon.mined[str(unknown)] = None

        proc = _run(daemon, home, [str(root), "--json"])

        assert proc.returncode == 0, proc.stderr[-400:]
        assert set(_posted_dirs()) == {str(stale), str(never)}
        assert len(_Daemon.posts) == 2
        report = json.loads(proc.stdout)
        assert (report["queued"], report["undecidable"], report["current"]) == (2, 1, 1)


# ── 3. refusals, dry runs and the exit-code contract ───────────────────


class TestRefusals:
    def test_a_linked_worktree_is_refused_and_the_main_checkout_is_named(
        self, daemon, scene, tmp_path
    ):
        root, docs, home = scene
        main = tmp_path / "Projects" / "main-checkout"
        (root / ".git").rmdir()
        (root / ".git").write_text("gitdir: %s/.git/worktrees/wt\n" % main)
        assert cli._in_linked_worktree(str(root)) is True

        proc = _run(daemon, home, [str(root)])

        assert proc.returncode == 2
        assert _Daemon.posts == []
        assert str(main) in proc.stderr, proc.stderr

    def test_an_empty_palace_answer_is_refused_without_force(self, daemon, scene):
        root, docs, home = scene
        _Daemon.mined = {}

        proc = _run(daemon, home, [str(root), "--json"])

        assert proc.returncode == 2
        assert _Daemon.posts == []
        report = json.loads(proc.stdout)
        assert report["source"] == "cli"
        assert (report["wing"], report["enumerated"]) == ("proj_x", 4)

    def test_force_queues_every_enumerated_doc(self, daemon, scene):
        root, docs, home = scene
        _Daemon.mined = {}

        proc = _run(daemon, home, [str(root), "--force", "--json"])

        assert proc.returncode == 0, proc.stderr[-400:]
        assert set(_posted_dirs()) == {str(p) for p in docs.values()}
        assert json.loads(proc.stdout)["queued"] == 4

    def test_dry_run_posts_nothing_and_lists_the_plan(self, daemon, scene):
        root, docs, home = scene
        never = docs["docs/deep/b.md"]
        del _Daemon.mined[str(never)]

        proc = _run(daemon, home, [str(root), "--dry-run"])

        assert _Daemon.posts == []
        assert proc.returncode == 0
        assert "would queue      1 file(s)" in proc.stdout
        assert "docs/deep/b.md" in proc.stdout
        assert "Nothing has been queued" in proc.stdout

    def test_dry_run_json_reports_would_queue(self, daemon, scene):
        root, docs, home = scene
        del _Daemon.mined[str(docs["CLAUDE.md"])]
        proc = _run(daemon, home, [str(root), "--dry-run", "--json"])
        assert _Daemon.posts == []
        report = json.loads(proc.stdout)
        assert report["dry_run"] is True
        assert report["would_queue"] == 1
        assert report["files"] == [str(docs["CLAUDE.md"])]

    def test_a_daemon_refusal_on_mine_exits_2(self, daemon, scene):
        root, docs, home = scene
        del _Daemon.mined[str(docs["CLAUDE.md"])]
        _Daemon.mine_status = 400

        proc = _run(daemon, home, [str(root), "--json"])

        assert len(_Daemon.posts) == 1, "the request must have been attempted"
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["failed"] == 1

    def test_a_missing_directory_exits_2(self, daemon, scene):
        root, docs, home = scene
        proc = _run(daemon, home, [str(root / "nope")])
        assert proc.returncode == 2
        # argparse also exits 2 for an unknown verb, so the code alone would
        # pass on a tree without the verb (measured: it did, on origin/main).
        assert "is not a directory" in proc.stderr, proc.stderr
        assert _Daemon.posts == []


class TestJsonOutputIsOneDocument:
    def test_stdout_parses_when_mines_were_posted(self, daemon, scene):
        """The poster prints a receipt per file. In `--json` mode that receipt
        must not land in front of the document, or `json.loads(stdout)` fails
        for every consumer that queued anything — which is every useful run."""
        root, docs, home = scene
        _Daemon.mined = {}
        proc = _run(daemon, home, [str(root), "--force", "--json"])
        json.loads(proc.stdout)  # raises on the defect
        assert "Daemon mine" not in proc.stdout
        assert proc.stdout.lstrip().startswith("{")
