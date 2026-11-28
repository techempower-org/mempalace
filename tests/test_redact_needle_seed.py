"""A writer hands its vault needles to the mine children it spawns, and refreshes them by item revision.

Flow 3a002409 (2026-11-27): every ``mempalace mine`` child fetched every configured vault item itself
(``bw get password``, serially, 30 s each). Under a re-mine burst one fetch took ~8 s and some passed
30 s, so a child with no last good value failed closed and filed nothing. palace-daemon already holds
good values (its refresher). It now passes them to each child through an inherited pipe: never argv,
environ, disk or a log. The refresher reads each item's revisionDate from the vault CLI's local copy (no
CLI call, no decryption) and fetches a value only when its revision moved, it is missing, or it passes a
backstop well under the max-keep; any doubt about the copy fetches every item, as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

import mempalace.redact as R
from mempalace.redact import SecretPatternsUnavailable, redact

# Built at run time; lower case only, so no shape rule catches them and only a needle can.
VALUES = {"item-a": "seed" + "valuealpha", "item-b": "seed" + "valuebravo"}
FETCHED = (
    "fetched" + "valuezulu"
)  # what the fake vault CLI answers, so a fetch is visible in output


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(
        json.dumps(
            {
                "secret_vault_items": [
                    {"id": "item-a", "label": "alpha"},
                    {"id": "item-b", "label": "bravo"},
                ]
            }
        )
    )
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture(autouse=True)
def _fresh():
    def reset():
        R.stop_vault_refresher()
        R._vault_values.clear()
        R._seeded.clear()
        R._rev_seen.clear()
        R._revisions.update(sig=None, ids=None, revs=None, parses=0)
        R._cache["key"] = None
        R._cache["expires_at"] = None

    reset()
    yield
    reset()


@pytest.fixture
def fake_bw(tmp_path):
    """A ``bw`` on PATH that logs each call's argv and answers a fixed value, or a status."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "bw-calls.log"
    script = bindir / "bw"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$*" >> {str(log)!r}
            case "$*" in
              *status*) echo '{{"status":"unlocked","lastSync":"2026-11-27T23:06:52.700Z"}}' ;;
              *) printf '%s\\n' {FETCHED!r} ;;
            esac
            """
        )
    )
    script.chmod(0o755)
    return bindir, log


# The child: read one line of text on stdin, print it redacted, report what it can see of the seed,
# then wait for a second line so the test can read its /proc entries while it is alive.
CHILD = textwrap.dedent(
    """\
    import json, os, sys
    import mempalace.redact as R
    text = sys.stdin.readline()
    out = R.redact(text)
    print(json.dumps({"out": out, "env_has_fd": "MEMPALACE_NEEDLE_FD" in os.environ,
                      "pid": os.getpid()}), flush=True)
    sys.stdin.readline()
    """
)


def _spawn_child(fake_bw, _config, seed_fd=None):
    bindir, log = fake_bw
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "MEMPALACE_CONFIG_DIR": str(_config),
        "HOME": os.environ.get("HOME", "/"),
        # only the tree under test: mempalace strips PYTHONPATH entries from sys.path at import
        "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(R.__file__))),
    }
    kwargs = {}
    if seed_fd is not None:
        env[R.NEEDLE_SEED_ENV] = str(seed_fd)
        kwargs["pass_fds"] = (seed_fd,)
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **kwargs,
    )
    if seed_fd is not None:
        os.close(seed_fd)  # the parent's copy goes as soon as the child holds its own
    return proc


def _seed_parent(age=30.0):
    now = R.time.monotonic()
    for item_id, value in VALUES.items():
        R._vault_values[item_id] = (value, now - age)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="/proc and a shared CLOCK_MONOTONIC"
)
def test_a_seeded_child_makes_no_vault_call_and_no_value_reaches_its_argv_environ_or_log(
    fake_bw, _config
):
    _seed_parent()
    fd = R.needle_seed_fd()
    assert fd is not None
    child = _spawn_child(fake_bw, _config, fd)
    try:
        text = f"pw {VALUES['item-a']} and {VALUES['item-b']}\n"
        child.stdin.write(text.encode())
        child.stdin.flush()
        seen = json.loads(child.stdout.readline())
        pid = seen["pid"]
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
        with open(f"/proc/{pid}/environ", "rb") as f:
            environ = f.read()
        open_fds = os.listdir(f"/proc/{pid}/fd")
    finally:
        child.stdin.write(b"\n")
        child.stdin.close()
        err = child.stderr.read()
        child.wait(timeout=30)
    assert seen["out"] == "pw [redacted vault alpha] and [redacted vault bravo]\n"
    assert not fake_bw[1].exists(), "the child called the vault CLI"
    assert not seen["env_has_fd"], "the fd number must not reach the child's own children"
    assert str(fd) not in open_fds, "the child keeps the seed pipe open after reading it"
    for value in VALUES.values():
        for where, blob in (("argv", cmdline), ("environ", environ), ("log", err)):
            assert value.encode() not in blob, f"a seeded value is in the child's {where}"


def test_a_child_without_the_fd_fetches_as_today(fake_bw, _config):
    # The positive control for the test above: the fake CLI does see a child's fetches.
    child = _spawn_child(fake_bw, _config)
    out, err = child.communicate(f"pw {FETCHED}\n\n".encode(), timeout=60)
    assert out, err.decode()[-3000:]
    seen = json.loads(out.decode().splitlines()[0])
    assert seen["out"] == "pw [redacted vault alpha]\n"  # both items answer FETCHED; alpha is first
    calls = fake_bw[1].read_text().splitlines()
    assert calls == ["--nointeraction get password item-a", "--nointeraction get password item-b"]


def test_the_seed_carries_the_parents_fetch_time_so_max_keep_still_fails_closed(monkeypatch):
    _seed_parent(age=R._VAULT_MAX_KEEP - 2)
    payload = R._seed_payload()
    R._vault_values.clear()
    assert R._apply_seed(payload) == 2
    monkeypatch.setattr(
        R, "_vault_fetch", lambda _i: (_ for _ in ()).throw(AssertionError("fetch"))
    )
    assert redact(f"x {VALUES['item-a']} y") == "x [redacted vault alpha] y"  # no fetch: vouched
    # Past the max-keep the seeded value fails closed exactly like one the child fetched itself.
    for item_id in VALUES:
        value, t = R._vault_values[item_id]
        R._vault_values[item_id] = (value, t - 5)

    def locked(_i):
        raise SecretPatternsUnavailable("vault answered rc 1, empty")

    monkeypatch.setattr(R, "_vault_fetch", locked)
    R._cache["key"] = None
    with pytest.raises(SecretPatternsUnavailable):
        redact("anything")


def test_the_parent_never_seeds_a_value_past_the_max_keep():
    now = R.time.monotonic()
    R._vault_values["item-a"] = (VALUES["item-a"], now - R._VAULT_MAX_KEEP - 1)
    R._vault_values["item-b"] = (VALUES["item-b"], now - 10)
    items = json.loads(R._seed_payload())["items"]
    assert list(items) == ["item-b"]


def test_no_values_means_no_seed():
    assert R._seed_payload() is None
    assert R.needle_seed_fd() is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        json.dumps({"v": 99, "items": {}}).encode(),
        json.dumps({"v": 1, "items": {"item-a": ["short", 1.0]}}).encode(),
        json.dumps({"v": 1, "items": {"item-a": [VALUES["item-a"], 1e30]}}).encode(),  # future
        json.dumps({"v": 1, "items": {"item-a": [VALUES["item-a"], "x"]}}).encode(),
        json.dumps({"v": 1, "items": ["item-a"]}).encode(),
    ],
)
def test_a_malformed_seed_is_dropped_whole_and_logs_no_content(payload, caplog):
    assert R._apply_seed(payload) == 0
    assert R._vault_values == {} and R._seeded == set()
    assert VALUES["item-a"] not in caplog.text and "short" not in caplog.text


def test_a_seed_fd_that_is_not_a_pipe_is_left_alone(tmp_path, monkeypatch):
    f = tmp_path / "plain"
    f.write_bytes(json.dumps({"v": 1, "items": {i: [v, 1.0] for i, v in VALUES.items()}}).encode())
    fd = os.open(f, os.O_RDONLY)
    try:
        monkeypatch.setenv(R.NEEDLE_SEED_ENV, str(fd))
        assert R._seed_from_env() == 0
        os.fstat(fd)  # still open: not ours to close
        assert R.NEEDLE_SEED_ENV not in os.environ
    finally:
        os.close(fd)


def test_the_seed_is_read_from_a_pipe_and_the_pipe_closed(monkeypatch):
    _seed_parent()
    fd = R.needle_seed_fd()
    R._vault_values.clear()
    monkeypatch.setenv(R.NEEDLE_SEED_ENV, str(fd))
    assert R._seed_from_env() == 2
    with pytest.raises(OSError):
        os.fstat(fd)
    assert R._seeded == set(VALUES)
    assert R.NEEDLE_SEED_ENV not in os.environ


# --- the refresher reads each item's revision from the CLI's local copy ------------------------------

# Built at run time: an encrypted-field stand-in that must never come out of the projection.
CANARY = "2." + "Q0FOQVJZ" * 4 + "|" + "ZW5j" * 6
NUM_CANARY = 918273645  # where the real copy holds a number
LAST_SYNC = "2026-11-27T23:06:52.700Z"


def _cipher(item_id, rev):
    """Every field but id and revisionDate carries a canary: strings, nested, numeric, and the fields the
    real copy holds as null or a boolean."""
    return {
        "id": item_id,
        "revisionDate": rev,
        "name": CANARY,
        "notes": CANARY,
        "key": CANARY,
        "login": {
            "username": CANARY,
            "password": CANARY,
            "totp": CANARY,
            "passwordRevisionDate": CANARY,
            "uris": [{"uri": CANARY, "match": NUM_CANARY}],
            "fido2Credentials": [{"keyValue": CANARY, "counter": NUM_CANARY}],
        },
        "fields": [{"name": CANARY, "value": CANARY, "type": NUM_CANARY, "linkedId": CANARY}],
        "passwordHistory": [{"password": CANARY, "lastUsedDate": CANARY}],
        "attachments": [{"fileName": CANARY, "key": CANARY, "size": CANARY}],
        "collectionIds": [CANARY],
        "folderId": CANARY,
        "organizationId": CANARY,
        "deletedDate": CANARY,
        "archivedDate": CANARY,
        "creationDate": CANARY,
        "permissions": {"delete": CANARY, "restore": CANARY},
        "favorite": CANARY,
        "edit": CANARY,
        "viewPassword": CANARY,
        "organizationUseTotp": CANARY,
        "reprompt": NUM_CANARY,
        "type": NUM_CANARY,
    }


def _write_data(path, revs, extra=True):
    ciphers = {i: _cipher(i, r) for i, r in revs.items()}
    if extra:
        ciphers["other-1"] = _cipher("other-1", "2026-01-02T00:00:00.000Z")
    doc = {
        "global_account_accounts": {"u-1": {"email": CANARY}},
        "user_0b1c-uid_profile_lastSync": LAST_SYNC,
        "user_0b1c-uid_ciphers_ciphers": ciphers,
        "user_0b1c-uid_folders_folders": {
            "f-1": {"id": "f-1", "revisionDate": "x", "name": CANARY}
        },
        "user_0b1c-uid_crypto_privateKey": CANARY,
    }
    path.write_text(json.dumps(doc))


@pytest.fixture
def cli_copy(tmp_path, monkeypatch):
    appdata = tmp_path / "Bitwarden CLI"
    appdata.mkdir()
    monkeypatch.setenv("BITWARDENCLI_APPDATA_DIR", str(appdata))
    data = appdata / "data.json"
    revs = {"item-a": "2026-11-27T18:15:09.966Z", "item-b": "2026-11-20T08:00:00.000Z"}
    _write_data(data, revs)
    R._revisions.update(sig=None, ids=None, revs=None, parses=0)
    return data, revs


@pytest.fixture
def gets(monkeypatch):
    calls = []

    def fetch(item_id):
        calls.append(item_id)
        return VALUES[item_id]

    monkeypatch.setattr(R, "_vault_fetch", fetch)
    return calls


def test_the_projection_holds_exactly_the_ids_and_their_revisions(cli_copy):
    data, revs = cli_copy
    assert CANARY in data.read_text() and str(NUM_CANARY) in data.read_text()  # the fixture can see
    out = R._vault_revisions(("item-a", "item-b"))
    assert out == {"item-a": "2026-11-27T18:15:09.966Z", "item-b": "2026-11-20T08:00:00.000Z"}
    assert out == revs
    for kept in (out, R._revisions):
        assert CANARY not in repr(kept) and str(NUM_CANARY) not in repr(kept)
    assert "other-1" not in repr(R._revisions) and LAST_SYNC not in repr(R._revisions)


def test_the_object_hook_drops_every_field_but_id_and_revision():
    item = json.loads(json.dumps(_cipher("x", "r")), object_pairs_hook=R._keep_revision_keys)
    assert item == {"id": "x", "revisionDate": "r"}
    container = json.loads(
        json.dumps({"a": CANARY, "b": [CANARY], "c": {"d": CANARY}}),
        object_pairs_hook=R._keep_revision_keys,
    )
    assert container == {"c": {}}


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps({"user_u_ciphers_ciphers": {}}),  # an id missing
        json.dumps({"something_else": {}}),  # unknown shape
        json.dumps(
            {"user_u_ciphers_ciphers": {"item-a": {"id": "item-a", "revisionDate": "r"}}}
            | {"user_v_ciphers_ciphers": {}}
        ),  # two accounts: ambiguous
        json.dumps({"user_u_ciphers_ciphers": {"item-a": {"id": "item-z", "revisionDate": "r"}}}),
    ],
)
def test_any_doubt_about_the_copy_fetches_every_item(cli_copy, gets, body):
    data, _revs = cli_copy
    data.write_text(body)
    assert R._vault_revisions(("item-a",)) is None
    for _ in range(3):
        assert R.refresh_vault_needles() == {"alpha": "ok", "bravo": "ok"}
    assert gets == ["item-a", "item-b"] * 3


def test_a_missing_copy_fetches_every_item(cli_copy, gets):
    data, _revs = cli_copy
    data.unlink()
    R.refresh_vault_needles()
    R.refresh_vault_needles()
    assert gets == ["item-a", "item-b"] * 2


def test_no_get_while_revisions_are_unchanged_and_every_value_is_under_the_backstop(cli_copy, gets):
    assert R.refresh_vault_needles() == {"alpha": "ok", "bravo": "ok"}  # no values yet: fetch
    assert gets == ["item-a", "item-b"]
    gets.clear()
    for _ in range(5):  # a window of refreshes over an unchanged local copy
        report = R.refresh_vault_needles()
        assert all(r.startswith("unchanged") for r in report.values()), report
    assert gets == []


def test_the_copy_is_parsed_only_when_its_mtime_or_size_changes(cli_copy, gets):
    data, revs = cli_copy
    for _ in range(4):
        R.refresh_vault_needles()
    assert R._revisions["parses"] == 1
    st = data.stat()
    os.utime(data, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # the CLI rewrote it as is
    R.refresh_vault_needles()
    assert R._revisions["parses"] == 2
    gets.clear()
    R.refresh_vault_needles()
    assert gets == [] and R._revisions["parses"] == 2


def test_a_local_edit_moves_one_revision_and_fetches_only_that_item(cli_copy, gets):
    # The hole the lastSync gate had: `bw edit` on this host changes the item and its revisionDate in
    # the local copy, and moves no lastSync.
    data, revs = cli_copy
    R.refresh_vault_needles()
    gets.clear()
    _write_data(data, {**revs, "item-b": "2026-11-27T19:30:00.000Z"})
    assert json.loads(data.read_text())["user_0b1c-uid_profile_lastSync"] == LAST_SYNC  # unchanged
    report = R.refresh_vault_needles()
    assert report["bravo"] == "ok" and report["alpha"].startswith("unchanged"), report
    assert gets == ["item-b"]


def test_the_backstop_fetches_an_old_value_with_its_revision_unchanged(cli_copy, gets):
    R.refresh_vault_needles()
    gets.clear()
    value, t = R._vault_values["item-b"]
    R._vault_values["item-b"] = (value, t - R._VAULT_BACKSTOP - 1)
    R.refresh_vault_needles()
    assert gets == ["item-b"]
    assert R._VAULT_BACKSTOP < R._VAULT_MAX_KEEP / 2


def test_a_missing_value_is_fetched(cli_copy, gets):
    R.refresh_vault_needles()
    gets.clear()
    del R._vault_values["item-a"]
    R.refresh_vault_needles()
    assert gets == ["item-a"]


def test_a_failed_get_is_retried_next_refresh_and_only_that_item(cli_copy, monkeypatch):
    calls = []
    flaky = {"fail": True}

    def fetch(item_id):
        calls.append(item_id)
        if item_id == "item-b" and flaky["fail"]:
            raise SecretPatternsUnavailable("vault unreachable (TimeoutExpired)")
        return VALUES[item_id]

    monkeypatch.setattr(R, "_vault_fetch", fetch)
    report = R.refresh_vault_needles()
    assert report["alpha"] == "ok" and report["bravo"].startswith("none")
    calls.clear()
    flaky["fail"] = False
    R.refresh_vault_needles()
    assert calls == ["item-b"]


def test_the_refresher_makes_no_periodic_cli_call_and_never_syncs(cli_copy, monkeypatch):
    argvs = []

    class Done:
        returncode = 0

    def run(argv, **_kw):
        argvs.append(argv[1:])
        out = Done()
        out.stdout = VALUES[argv[-1]].encode() + b"\n"
        return out

    monkeypatch.setattr(subprocess, "run", run)
    for _ in range(4):
        R.refresh_vault_needles()
    assert argvs == [
        ["--nointeraction", "get", "password", "item-a"],
        ["--nointeraction", "get", "password", "item-b"],
    ]


def test_the_copy_is_opened_read_only(cli_copy, monkeypatch):
    import builtins

    modes = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **kw):
        if str(file).endswith("data.json"):
            modes.append(mode)
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy)
    R._vault_revisions(("item-a",))
    assert modes == ["rb"]


# --- the writer never blocks on the pipe (lead ruling 1) ---------------------------------------------


def _small_pipe(prefill=0):
    """os.pipe with its capacity cut to 4 KiB (one page, F_SETPIPE_SZ), optionally holding ``prefill``
    bytes already, as under a per-user pipe-page limit or a reader that never drained."""
    import fcntl

    real_pipe = os.pipe

    def pipe():
        r, w = real_pipe()
        fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)
        assert fcntl.fcntl(w, fcntl.F_GETPIPE_SZ) == 4096
        if prefill:
            os.write(w, b"x" * prefill)
        return r, w

    return pipe


def _seed_in_thread():
    import threading

    out = {}
    t = threading.Thread(target=lambda: out.update(fd=R.needle_seed_fd()), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "needle_seed_fd blocked on the pipe"
    return out["fd"]


def _value_for_payload(size):
    """A needle value that makes the seed exactly ``size`` bytes."""
    R._vault_values["item-a"] = ("v" * 10, R.time.monotonic() - 1)
    base = len(R._seed_payload()) - 10
    value = "v" * (size - base)
    R._vault_values["item-a"] = (value, R._vault_values["item-a"][1])
    assert len(R._seed_payload()) == size
    return value


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="F_SETPIPE_SZ")
def test_a_full_pipe_buf_seed_fits_a_4k_pipe(monkeypatch):
    _value_for_payload(4096)
    monkeypatch.setattr(os, "pipe", _small_pipe())
    fd = _seed_in_thread()
    assert fd is not None
    try:
        assert len(os.read(fd, 65536)) == 4096
        assert os.read(fd, 1) == b""  # the write end is closed: EOF
    finally:
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="F_SETPIPE_SZ")
def test_a_pipe_that_cannot_take_the_seed_whole_means_no_seed_never_a_wait(monkeypatch, caplog):
    _value_for_payload(4000)
    monkeypatch.setattr(os, "pipe", _small_pipe(prefill=200))  # 3,896 bytes free
    assert _seed_in_thread() is None
    assert "BlockingIOError" in caplog.text
    assert "v" * 100 not in caplog.text


def test_a_seed_over_pipe_buf_is_never_attempted(monkeypatch):
    _value_for_payload(4097)
    monkeypatch.setattr(os, "pipe", lambda: pytest.fail("a pipe was made for an oversized seed"))
    assert R.needle_seed_fd() is None


# --- a real mine, seeded through the fd (lead rulings 2 and 3) ---------------------------------------

# The mine child: the test suite's deterministic embedding (no model download), the real CLI, its own
# process. mempalace.redact reads the seed at import, as in production.
MINE_CHILD = textwrap.dedent(
    """\
    import hashlib, logging, sys
    log_file, palace, src, mode = sys.argv[1:5]
    logging.basicConfig(filename=log_file, level=logging.DEBUG, force=True)
    import mempalace.redact  # takes the seed here, if one was passed

    DIM = 384

    def emb(text):
        vec = [0.0] * DIM
        for tok in str(text).lower().split():
            d = hashlib.sha256(tok.encode()).digest()
            vec[int.from_bytes(d[:4], "little") % DIM] += 1.0
        n = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / n for v in vec]

    class EF:
        @staticmethod
        def name():
            return "default"
        @staticmethod
        def build_from_config(config):
            return EF()
        @staticmethod
        def validate_config(config):
            return
        def get_config(self):
            return {}
        def is_legacy(self):
            return False
        def default_space(self):
            return "cosine"
        def supported_spaces(self):
            return ["cosine", "l2", "ip"]
        def embed_query(self, input):
            return self(input=input)
        def __call__(self, input):
            return [emb(t) for t in list(input or [])]

    ef = EF()
    import mempalace.backends.chroma as chroma_mod
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    import mempalace.embedding as embedding_mod
    from chromadb.api.types import DefaultEmbeddingFunction
    DefaultEmbeddingFunction.__call__ = lambda self, input: ef(input=input)
    DefaultEmbeddingFunction.embed_query = lambda self, input: ef(input=input)
    embedding_mod.get_embedding_function = lambda *_, **__: ef
    chroma_mod.ChromaBackend._resolve_embedding_function = staticmethod(lambda **_: ef)
    embedding_wrapper._embed_texts = lambda texts, **_: ef(input=list(texts))

    import mempalace.cli as cli
    sys.argv = ["mempalace", "--palace", palace, "mine", src, "--mode", mode, "--wing", "w"]
    cli.main()
    """
)


def _pipe_holding(payload: bytes) -> int:
    r, w = os.pipe()
    os.write(w, payload)
    os.close(w)
    return r


def _bw_that_fails(bindir, log):
    script = bindir / "bw"
    script.write_text(f'#!/bin/sh\necho "$*" >> {str(log)!r}\nexit 1\n')
    script.chmod(0o755)


def _run_mine(
    tmp_path, items, source_text, seed_fd=None, bw="fails", mode="projects", name="notes.md"
):
    """One real ``mempalace mine`` subprocess over a one-file source. Returns its result and paths."""
    root = tmp_path / "mine"
    home, cfg, palace, src, bindir = (root / n for n in ("home", "config", "palace", "src", "bin"))
    for d in (home, cfg, palace, src, bindir):
        d.mkdir(parents=True)
    (cfg / "config.json").write_text(
        json.dumps({"palace_path": str(palace), "secret_vault_items": items})
    )
    (src / name).write_text(source_text, encoding="utf-8")
    calls = root / "bw-calls.log"
    if bw == "fails":
        _bw_that_fails(bindir, calls)
    else:
        script = bindir / "bw"
        script.write_text(f'#!/bin/sh\necho "$*" >> {str(calls)!r}\nprintf "%s\\n" {bw!r}\n')
        script.chmod(0o755)
    log_file, phase = root / "mine.log", root / "phase.json"
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "MEMPALACE_CONFIG_DIR": str(cfg),
        "MEMPALACE_HUB_FORWARD": "0",
        "MEMPALACE_MINE_PHASE_FILE": str(phase),
        "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(R.__file__))),
    }
    kwargs = {}
    if seed_fd is not None:
        env[R.NEEDLE_SEED_ENV] = str(seed_fd)
        kwargs["pass_fds"] = (seed_fd,)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", MINE_CHILD, str(log_file), str(palace), str(src), mode],
            capture_output=True,
            env=env,
            timeout=180,
            **kwargs,
        )
    finally:
        if seed_fd is not None:
            os.close(seed_fd)
    return proc, {
        "root": root,
        "palace": palace,
        "src": src,
        "calls": calls,
        "log": log_file,
        "phase": phase,
    }


def _drawers(palace):
    from mempalace.palace import get_collection

    try:
        col = get_collection(str(palace), create=False)
    except Exception:  # noqa: BLE001 -- no collection: nothing was filed
        return []
    return col.get(include=["documents"])["documents"]


TWO_ITEMS = [{"id": "item-a", "label": "alpha"}, {"id": "item-b", "label": "bravo"}]
BODY = (
    "# notes\n\n"
    + " ".join(f"line{j} about the garden shed and its roof" for j in range(60))
    + "\n"
)
REFUSED = "vault answered rc 1, empty"


def _seed_bytes(items):
    return json.dumps({"v": 1, "items": items}).encode()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pass_fds, shared CLOCK_MONOTONIC")
@pytest.mark.parametrize("variant", ["stale", "missing-id", "malformed"])
def test_a_bad_seed_makes_the_mine_refuse_to_file_as_a_vault_failure_does(tmp_path, variant):
    # Today's outcome when the vault cannot answer: no seed, the CLI fails, the mine refuses to file.
    base, base_paths = _run_mine(tmp_path / "base", TWO_ITEMS, BODY)
    assert base.returncode != 0
    assert REFUSED in base.stderr.decode() and "SecretPatternsUnavailable" in base.stderr.decode()
    assert _drawers(base_paths["palace"]) == []

    now = R.time.monotonic()
    payload = {
        "stale": _seed_bytes({i: [v, now - R._VAULT_MAX_KEEP - 60] for i, v in VALUES.items()}),
        "missing-id": _seed_bytes({"item-a": [VALUES["item-a"], now - 5]}),
        "malformed": b'{"v": 1, "items": {"item-a": ["seedvalue',
    }[variant]
    proc, paths = _run_mine(tmp_path / variant, TWO_ITEMS, BODY, seed_fd=_pipe_holding(payload))
    err = proc.stderr.decode()
    assert proc.returncode == base.returncode, err[-3000:]
    assert "SecretPatternsUnavailable" in err and REFUSED in err, err[-3000:]
    assert _drawers(paths["palace"]) == []
    assert paths["calls"].exists()  # it went to the (failing) vault for what the seed did not give


CANARY_NEEDLE = "canary" + "minesecret" + "q"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pass_fds, shared CLOCK_MONOTONIC")
def test_a_seeded_mine_keeps_the_needle_out_of_everything_it_writes(tmp_path):
    R._vault_values["item-c"] = (CANARY_NEEDLE, R.time.monotonic() - 5)
    fd = R.needle_seed_fd()
    R._vault_values.clear()
    # A session transcript, mined in convos mode as the daemon's hook mines are: that miner writes the
    # heartbeat (phase file) the daemon reads.
    turns = []
    for j in range(12):
        turns.append(
            {"type": "user", "message": {"role": "user", "content": f"q{j} about the shed roof"}}
        )
        say = f"a{j}: the shed roof needs work" + (
            f"; the shed wifi password is {CANARY_NEEDLE}" if j % 4 == 1 else ""
        )
        turns.append(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": say}]},
            }
        )
    text = "".join(json.dumps(t) + "\n" for t in turns)
    # The fake CLI answers another value: a fetch would leave the canary unredacted and show in its log.
    proc, paths = _run_mine(
        tmp_path,
        [{"id": "item-c", "label": "shed-wifi"}],
        text,
        seed_fd=fd,
        bw="fetched" + "othervalue",
        mode="convos",
        name="session.jsonl",
    )
    err = proc.stderr.decode()
    assert proc.returncode == 0, err[-3000:]
    assert not paths["calls"].exists(), "the seeded mine called the vault CLI"
    docs = _drawers(paths["palace"])
    assert any("[redacted vault shed-wifi]" in d for d in docs)  # the needle was applied
    needle = CANARY_NEEDLE.encode()
    assert needle not in proc.stdout and needle not in proc.stderr  # the mine's report
    assert paths["phase"].exists() and needle not in paths["phase"].read_bytes()  # the heartbeat
    assert paths["log"].exists() and needle not in paths["log"].read_bytes()  # the log file
    scanned, hits = 0, []
    for p in paths["root"].rglob("*"):
        if p.is_file() and paths["src"] not in p.parents:
            scanned += 1
            if needle in p.read_bytes():
                hits.append(str(p.relative_to(paths["root"])))
    assert hits == [], hits  # palace, config, home, phase, log: no written state holds it
    assert scanned > 5
    # The scan can see: the one place the canary was put is found.
    assert any(needle in p.read_bytes() for p in paths["src"].rglob("*") if p.is_file())
