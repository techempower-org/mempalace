"""--key-file: a dream run reads its keys from 0600 env files, never through systemd EnvironmentFile=."""

import os
import secrets

from mempalace.dream import keys
from mempalace.dream import __main__ as dream_main


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_a_key_the_environment_lacks_is_read_from_the_first_file_that_has_it(tmp_path):
    k1, k2 = secrets.token_hex(12), secrets.token_hex(12)
    a = _write(tmp_path / "a.env", '# daemon\nOTHER=1\nexport PALACE_API_KEY="%s"\n' % k1)
    b = _write(tmp_path / "b.env", "PALACE_API_KEY=wrong\nDREAM_LLM_API_KEY='%s'\n" % k2)
    env = {}
    assert keys.load([a, b], environ=env) == ["PALACE_API_KEY", "DREAM_LLM_API_KEY"]
    assert env == {"PALACE_API_KEY": k1, "DREAM_LLM_API_KEY": k2}


def test_the_environment_wins_and_a_missing_file_is_no_error(tmp_path):
    env = {"PALACE_API_KEY": "set"}
    f = _write(tmp_path / "a.env", "PALACE_API_KEY=other\n")
    assert keys.load([tmp_path / "missing.env", f], environ=env) == []
    assert env == {"PALACE_API_KEY": "set"}


def test_a_value_with_equals_signs_is_kept_whole(tmp_path):
    v = secrets.token_hex(8) + "==x=y"
    env = {}
    keys.load([_write(tmp_path / "a.env", "DREAM_LLM_API_KEY=%s\n" % v)], environ=env)
    assert env["DREAM_LLM_API_KEY"] == v


def test_the_cli_reads_key_files_before_the_run(tmp_path, monkeypatch):
    k = secrets.token_hex(12)
    f = _write(tmp_path / "d.env", "PALACE_API_KEY=%s\n" % k)
    monkeypatch.delenv("PALACE_API_KEY", raising=False)
    monkeypatch.delenv("DREAM_LLM_API_KEY", raising=False)
    seen = {}

    def fake_inventory(*a, **kw):
        seen["key"] = os.environ.get("PALACE_API_KEY")
        raise SystemExit(0)

    monkeypatch.setattr(dream_main, "_claude_md", fake_inventory)
    try:
        dream_main.main(
            [
                "inventory",
                "--memory-root",
                str(tmp_path),
                "--out",
                str(tmp_path / "out"),
                "--key-file",
                str(f),
            ]
        )
    except SystemExit:
        pass
    assert seen["key"] == k
