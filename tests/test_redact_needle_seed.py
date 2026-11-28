"""A writer hands its vault needles to the mine children it spawns, and refreshes them by vault state.

Flow 3a002409 (2026-11-27): every ``mempalace mine`` child fetched every configured vault item itself
(``bw get password``, serially, 30 s each). Under a re-mine burst one fetch took ~8 s and some passed
30 s, so a child with no last good value failed closed and filed nothing. palace-daemon already holds
good values (its refresher). It now passes them to each child through an inherited pipe: never argv,
environ, disk or a log. The refresher asks ``bw status`` (no secret) and fetches values only when the
vault's lastSync moved, a value is missing, or the oldest passes a backstop well under the max-keep.
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
        R._sync_seen.clear()
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


# --- the refresher asks the vault's state first --------------------------------------------------


@pytest.fixture
def counted(monkeypatch):
    calls = {"get": [], "status": 0}
    state = {"last_sync": "2026-11-27T23:06:52.700Z"}

    def fetch(item_id):
        calls["get"].append(item_id)
        return VALUES[item_id]

    def sync_state():
        calls["status"] += 1
        return state["last_sync"], "unlocked" if state["last_sync"] else "locked"

    monkeypatch.setattr(R, "_vault_fetch", fetch)
    monkeypatch.setattr(R, "_vault_sync_state", sync_state)
    return calls, state


def test_no_get_while_last_sync_is_unchanged_and_every_value_is_under_the_backstop(counted):
    calls, _state = counted
    assert R.refresh_vault_needles() == {"alpha": "ok", "bravo": "ok"}  # no values yet: fetch
    assert calls["get"] == ["item-a", "item-b"]
    calls["get"].clear()
    for _ in range(5):  # a window of refreshes over an unchanged vault
        report = R.refresh_vault_needles()
        assert all(r.startswith("unchanged") for r in report.values()), report
    assert calls["get"] == []
    assert calls["status"] == 6


def test_a_moved_last_sync_fetches_again(counted):
    calls, state = counted
    R.refresh_vault_needles()
    calls["get"].clear()
    state["last_sync"] = "2026-11-28T01:00:00.000Z"
    assert R.refresh_vault_needles() == {"alpha": "ok", "bravo": "ok"}
    assert calls["get"] == ["item-a", "item-b"]


def test_the_backstop_fetches_an_old_value_even_with_last_sync_unchanged(counted):
    calls, _state = counted
    R.refresh_vault_needles()
    calls["get"].clear()
    value, t = R._vault_values["item-b"]
    R._vault_values["item-b"] = (value, t - R._VAULT_BACKSTOP - 1)
    R.refresh_vault_needles()
    assert calls["get"] == ["item-b"]
    assert R._VAULT_BACKSTOP < R._VAULT_MAX_KEEP / 2


def test_a_missing_value_is_fetched(counted):
    calls, _state = counted
    R.refresh_vault_needles()
    calls["get"].clear()
    del R._vault_values["item-a"]
    R.refresh_vault_needles()
    assert calls["get"] == ["item-a"]


def test_a_status_that_cannot_answer_adds_no_gets_while_values_are_good(counted):
    calls, state = counted
    R.refresh_vault_needles()
    calls["get"].clear()
    state["last_sync"] = None  # locked, or the CLI timed out
    report = R.refresh_vault_needles()
    assert calls["get"] == []
    assert all(r.startswith("kept") for r in report.values()), report


def test_a_failed_get_is_retried_next_refresh_and_only_that_item(counted, monkeypatch):
    calls, _state = counted
    flaky = {"fail": True}

    def fetch(item_id):
        calls["get"].append(item_id)
        if item_id == "item-b" and flaky["fail"]:
            raise SecretPatternsUnavailable("vault unreachable (TimeoutExpired)")
        return VALUES[item_id]

    monkeypatch.setattr(R, "_vault_fetch", fetch)
    report = R.refresh_vault_needles()
    assert report["alpha"] == "ok" and report["bravo"].startswith("none")
    calls["get"].clear()
    flaky["fail"] = False
    R.refresh_vault_needles()
    assert calls["get"] == ["item-b"]


def test_the_refresher_never_runs_a_vault_sync(monkeypatch):
    argvs = []

    class Done:
        returncode = 0
        stdout = b'{"status":"unlocked","lastSync":"2026-11-27T23:06:52.700Z"}'

    def run(argv, **_kw):
        argvs.append(argv[1:])
        if "status" in argv:
            return Done()
        out = Done()
        out.stdout = VALUES[argv[-1]].encode() + b"\n"
        return out

    monkeypatch.setattr(subprocess, "run", run)
    for _ in range(3):
        R.refresh_vault_needles()
    assert ["--nointeraction", "status"] in argvs
    assert not any("sync" in a for argv in argvs for a in argv)
    assert sum(1 for a in argvs if a[:2] == ["--nointeraction", "get"]) == 2
