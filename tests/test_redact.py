"""Secrets are replaced by a placeholder before any backend stores a drawer (#596)."""

from __future__ import annotations

import hashlib

import pytest

from mempalace.redact import SecretPatternsUnavailable, redact, redact_metadatas

# Built at run time so the file itself holds no credential-shaped literal.
FAKE = {
    "anthropic": "sk-" + "ant-api03-" + "A1b2" * 10,
    "openai": "sk-" + "proj-" + "Zz9" * 16,
    "github": "gh" + "p_" + "Q" * 36,
    "slack": "xo" + "xp-1234-5678-" + "abcd" * 6,
    "aws": "AK" + "IA" + "Q7" * 8,
    "google": "AI" + "za" + "x" * 35,
    "notion": "nt" + "n_" + "N0" * 23,
}


def _placeholder(kind, value):
    return f"[redacted {kind} sha256:{hashlib.sha256(value.encode()).hexdigest()[:8]}]"


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(cfg))
    return cfg


@pytest.mark.parametrize("kind", sorted(FAKE))
def test_each_default_shape_becomes_its_placeholder(kind):
    value = FAKE[kind]
    out = redact(f"export TOKEN={value}\nnext line")
    assert value not in out
    assert out == f"export TOKEN={_placeholder(kind, value)}\nnext line"


def test_a_token_after_a_literal_newline_escape_is_found():
    # A raw JSONL line: the token follows "\n" with no word boundary.
    value = FAKE["notion"]
    assert value not in redact('{"text": "env:\\n' + value + '"}')


def test_text_without_secrets_is_returned_unchanged():
    text = "disk-usage task-list AKIA is a prefix, sk- alone too"
    assert redact(text) is text


def test_config_patterns_extend_the_defaults(_config):
    (_config / "config.json").write_text('{"secret_patterns": ["acme_[0-9]{8}"]}')
    # No hash for a configured pattern: its value may be guessable.
    assert redact("key acme_12345678 end") == "key [redacted custom] end"


@pytest.mark.parametrize(
    "body", ['{"secret_patterns": ["("]}', '{"secret_patterns": "x"}', "{not json"]
)
def test_a_bad_pattern_or_an_unreadable_config_fails_closed(_config, body):
    (_config / "config.json").write_text(body)
    with pytest.raises(SecretPatternsUnavailable):
        redact("anything")


def test_metadata_strings_are_redacted_without_touching_the_callers_dict():
    meta = {"aaak_source": "TOKEN=" + FAKE["notion"], "chunk_index": 3}
    (out,) = redact_metadatas([meta])
    assert FAKE["notion"] not in out["aaak_source"] and out["chunk_index"] == 3
    assert FAKE["notion"] in meta["aaak_source"]


def test_every_backend_write_redacts_before_the_backend_runs():
    from mempalace.backends.base import BaseCollection

    seen = []

    class _Store(BaseCollection):
        def add(self, *, documents, ids, metadatas=None, embeddings=None):
            seen.append(("add", documents, metadatas))

        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            seen.append(("upsert", documents, metadatas))

        def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
            seen.append(("update", documents, metadatas))

        query = get = delete = count = lambda self, **kw: None

    class _Subclass(_Store):
        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            seen.append(("sub-upsert", documents, metadatas))

    secret = FAKE["github"]
    s = _Store()
    s.add(documents=[f"a {secret}"], ids=["1"], metadatas=[{"m": secret}])
    s.upsert(documents=[f"b {secret}"], ids=["1"])
    s.update(ids=["1"], metadatas=[{"m": secret}])
    _Subclass().upsert(documents=f"c {secret}", ids=["1"])
    assert len(seen) == 4
    assert secret not in repr(seen)
    assert seen[0][1] == [f"a {_placeholder('github', secret)}"]


def test_a_real_backend_stores_the_placeholder(tmp_path):
    from mempalace.palace import get_collection

    col = get_collection(str(tmp_path / "palace"), create=True)
    value = FAKE["anthropic"]
    col.upsert(
        documents=[f"the key was {value} yesterday"],
        ids=["d1"],
        metadatas=[{"wing": "w", "room": "r", "aaak_source": value}],
    )
    got = col.get(ids=["d1"], include=["documents", "metadatas"])
    assert got["documents"] == [f"the key was {_placeholder('anthropic', value)} yesterday"]
    assert value not in repr(got["metadatas"])


def test_an_unchanged_record_holding_a_secret_is_still_skipped(tmp_path):
    from mempalace.palace import get_collection
    from mempalace.sources.context import _stored_matches

    col = get_collection(str(tmp_path / "palace"), create=True)
    content = f"config dump: {FAKE['slack']}"
    meta = {"wing": "w", "room": "r", "note": FAKE["slack"]}
    col.upsert(documents=[content], ids=["d1"], metadatas=[meta])
    assert _stored_matches(col, "d1", content, meta)


def test_openai_project_keys_with_punctuation_are_fully_redacted():
    for value in ("sk-" + "proj-" + "Ab_9-" * 12, "sk-" + "svcacct-" + "x-Y_z" * 10):
        out = redact(f"key={value} end")
        assert value not in out and out == f"key={_placeholder('openai', value)} end"


def test_placeholders_survive_a_pattern_matching_their_hex(_config):
    # A configured pattern that would match the digest must not rewrite a placeholder.
    (_config / "config.json").write_text('{"secret_patterns": ["[0-9a-f]{8}"]}')
    once = redact("tok " + FAKE["github"])
    assert redact(once) == once


def test_nested_metadata_values_are_redacted():
    meta = {"details": {"token": FAKE["aws"]}, "tokens": [FAKE["google"], 1]}
    (out,) = redact_metadatas([meta])
    assert FAKE["aws"] not in repr(out) and FAKE["google"] not in repr(out)
    assert out["tokens"][1] == 1 and FAKE["aws"] in meta["details"]["token"]


def test_a_write_with_nothing_to_scan_still_fails_on_a_broken_config(_config):
    from mempalace.backends.base import BaseCollection

    class _Store(BaseCollection):
        def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
            pass

        add = upsert = query = get = delete = count = lambda self, **kw: None

    (_config / "config.json").write_text('{"secret_patterns": ["("]}')
    with pytest.raises(SecretPatternsUnavailable):
        _Store().update(ids=["1"], embeddings=[[0.0]])


def test_positional_arguments_and_mixin_writes_are_redacted():
    from mempalace.backends.base import BaseCollection

    seen = []

    class _Mixin:
        def add(self, documents, ids, metadatas=None, embeddings=None):
            seen.append(documents)

    class _Store(_Mixin, BaseCollection):
        upsert = query = get = delete = count = lambda self, **kw: None

    _Store().add([FAKE["slack"]], ["1"])
    assert seen and FAKE["slack"] not in repr(seen)


def test_every_backend_class_wraps_its_writes():
    import importlib
    import pkgutil

    import mempalace.backends as backends
    from mempalace.backends.base import BaseCollection

    for info in pkgutil.iter_modules(backends.__path__):
        try:
            importlib.import_module(f"mempalace.backends.{info.name}")
        except ImportError:
            continue  # an optional client library is not installed

    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    found = [c for c in subclasses(BaseCollection) if c.__module__.startswith("mempalace.")]
    assert len(found) >= 3
    for cls in found:
        for name in ("add", "upsert", "update"):
            fn = getattr(cls, name)
            if not getattr(fn, "__isabstractmethod__", False):
                assert getattr(fn, "__redacts_secrets__", False), f"{cls.__name__}.{name}"


def test_writes_through_var_keyword_arguments_are_redacted():
    from mempalace.backends.base import BaseCollection

    seen = []

    class _Store(BaseCollection):
        def add(self, ids, **kwargs):
            seen.append(kwargs)

        upsert = query = get = delete = count = lambda self, **kw: None

    _Store().add(["1"], documents=[FAKE["aws"]], metadatas=[{"k": FAKE["aws"]}])
    assert seen and FAKE["aws"] not in repr(seen)


def test_a_placeholder_just_written_is_not_rewritten_by_a_later_pattern(_config):
    import re

    (_config / "config.json").write_text('{"secret_patterns": ["[0-9a-f]{8}", "redacted"]}')
    once = redact("id " + FAKE["aws"] + " end")
    assert once == f"id {_placeholder('aws', FAKE['aws'])} end"
    assert redact(once) == once
    assert re.fullmatch(r"id \[redacted aws sha256:[0-9a-f]{8}\] end", once)


def test_a_namedtuple_holding_a_secret_comes_back_as_a_plain_tuple():
    from collections import namedtuple

    Pair = namedtuple("Pair", "a b")
    (out,) = redact_metadatas([{"pair": Pair("x", FAKE["notion"])}])
    assert out["pair"] == ("x", _placeholder("notion", FAKE["notion"]))


def test_a_global_inline_flag_in_a_configured_pattern_fails_closed(_config):
    (_config / "config.json").write_text('{"secret_patterns": ["(?i)acme_[0-9]{8}"]}')
    with pytest.raises(SecretPatternsUnavailable):
        redact("anything")


def test_a_configured_pattern_cannot_take_only_part_of_a_provider_token(_config):
    (_config / "config.json").write_text('{"secret_patterns": ["TOKEN=[A-Za-z0-9_-]{10}"]}')
    value = "sk-" + "proj-" + "A" * 40
    out = redact("TOKEN=" + value)
    assert "AAAA" not in out and _placeholder("openai", value) in out


def test_a_default_argument_is_redacted():
    from mempalace.backends.base import BaseCollection

    seen = []
    secret = FAKE["google"]

    class _Store(BaseCollection):
        def add(self, documents=(secret,), ids=("1",), metadatas=None, embeddings=None):
            seen.append(documents)

        upsert = query = get = delete = count = lambda self, **kw: None

    _Store().add()
    assert seen and secret not in repr(seen)


def test_a_catch_all_named_documents_is_not_taken_for_the_documents_argument():
    from mempalace.backends.base import BaseCollection

    seen = []

    class _Store(BaseCollection):
        def add(self, **documents):
            seen.append(documents)

        upsert = query = get = delete = count = lambda self, **kw: None

    _Store().add(ids=["1"], documents=[FAKE["slack"]])
    assert seen[0]["ids"] == ["1"] and FAKE["slack"] not in repr(seen)


def test_configured_patterns_may_share_a_group_name(_config):
    (_config / "config.json").write_text(
        '{"secret_patterns": ["(?P<t>foo_[0-9]{6})", "(?P<t>bar_[0-9]{6})"]}'
    )
    assert redact("foo_123456 bar_654321") == "[redacted custom] [redacted custom]"


@pytest.mark.parametrize(
    "pattern",
    [r"(api_key)(=)(['\"])[^'\"]+\3", r"(a)?(?(1)b|c)", r"(k=)(?(+1)x|y)", "(a)?(?( \u0661)b|c)"],
)
def test_a_numbered_group_reference_fails_closed(_config, pattern):
    # "(?(+1)" and a non-ASCII digit compile only on Python <= 3.11; the guard rejects them first.
    import json

    (_config / "config.json").write_text(json.dumps({"secret_patterns": [pattern]}))
    with pytest.raises(SecretPatternsUnavailable):
        redact("api_key='bare-secret-value'")


# --- Values named as secrets (2026-11-07: a calcom env dump printed in a session) -------------

# A bare random value with no provider prefix, built at run time like FAKE.
BARE = "Q" + "w3rTy9Zx" * 3 + "Ab1Cd2Ef"
# Masked-looking secrets, also built at run time so a scanner reads no literal.
MASKED = "x" * 4 + "Actual" + "Secret" + "123"
A = "[redacted assignment]"


@pytest.mark.parametrize(
    "template",
    [
        "NEXTAUTH_SECRET={v}",
        "export EMAIL_SERVER_PASSWORD='{v}'",
        "  NEXTAUTH_SECRET: '{v}',",  # a JS object, as `node -e 'console.log(process.env)'` prints
        '{{"client_secret": "{v}"}}',
        '{{\\"password\\": \\"{v}\\"}}',  # a raw JSONL line: the quotes are escaped
        "db:\n  password: {v}\n",
        'connect(password="{v}")',
        "mysql --password={v} calcom",
        '"apiKey": "{v}"',
        "CALENDSO_ENCRYPTION_KEY = {v}",
        "GOOGLE_API_CREDENTIALS={v}",
        "DATABASE_URL: 'postgresql://calcom:{v}@localhost:5432/calcom'",
        "redis://default:{v}@cache:6379/0",
    ],
)
def test_a_value_named_as_a_secret_is_replaced_and_its_name_kept(template):
    out = redact(template.format(v=BARE))
    assert BARE not in out
    assert out == template.format(v=A)


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=128000 bypass=Abcdef12345 SECRET_FILE=/run/secrets/calcom",
        'password = getpass()  token = os.environ["X"]  secret = self.secret',
        "NEXTAUTH_SECRET: rotated, password: compromised, PASSWORD=${DB_PASSWORD}",
        "api_key=<your key> token=*** password: changeme PASSWORD=$(bw get password db)",
        "password: authenticationrequired  credentials = default_credentials_provider",
        '{{"password": "{{{{ vault_db_password }}}}"}}'.format(),
        "postgres://palace:${PW}@db  https://example.org:8443/path",
        "the passwords: we rotated all of them at 06:15",
    ],
)
def test_references_placeholders_prose_and_other_names_are_left_alone(text):
    assert redact(text) is text


def test_a_key_naming_no_secret_does_not_swallow_one_that_follows():
    assert redact(f"user: password={BARE}") == f"user: password={A}"


def test_a_provider_token_keeps_its_own_placeholder_and_a_second_pass_changes_nothing():
    value = FAKE["github"]
    once = redact(f"GITHUB_TOKEN={value} NEXTAUTH_SECRET={BARE}")
    assert once == f"GITHUB_TOKEN={_placeholder('github', value)} NEXTAUTH_SECRET={A}"
    assert redact(once) is once


def test_a_real_backend_stores_an_assignment_placeholder(tmp_path):
    from mempalace.palace import get_collection

    col = get_collection(str(tmp_path / "palace"), create=True)
    col.upsert(
        documents=[f"env dump:\nNEXTAUTH_SECRET={BARE}\nNODE_ENV=production"],
        ids=["d1"],
        metadatas=[{"wing": "w", "room": "r", "note": f"DATABASE_URL=postgres://u:{BARE}@h/db"}],
    )
    got = col.get(ids=["d1"], include=["documents", "metadatas"])
    assert got["documents"] == [f"env dump:\nNEXTAUTH_SECRET={A}\nNODE_ENV=production"]
    assert BARE not in repr(got["metadatas"])


def test_a_key_inside_a_value_already_replaced_is_not_redacted_twice():
    text = f"PASSWORD={BARE}:token={BARE[::-1]} next"
    assert redact(text) == f"PASSWORD={A} next"


# Strong forms (a quoted value, or KEY=value with nothing around the "=") redact whatever the value
# looks like; the cases a cross-model review found leaking (2026-11-07).
@pytest.mark.parametrize(
    "text, expected",
    [
        ("PASSWORD=12345678901234567890", f"PASSWORD={A}"),
        ("password=hunter2", f"password={A}"),
        ("NEXTAUTH_SECRET=abcdefghijklmnopqrs", f"NEXTAUTH_SECRET={A}"),
        (f"PASSWORD={MASKED}", f"PASSWORD={A}"),
        ("PASSWORD=$ecr3t!x", f"PASSWORD={A}"),
        ("PASSWORD=Abcd1234;Efgh5678 next", f"PASSWORD={A} next"),
        ("PASSWORD=Abcd1234,Efgh5678 next", f"PASSWORD={A} next"),
        ("PASSWORD=Abcd1234(Efgh5678) next", f"PASSWORD={A} next"),
        ('password="Abcd1234\'xyz" next', f'password="{A}" next'),
        ('{"password":"Abcd1234\\u0041"}', f'{{"password":"{A}"}}'),
        ("PASSWORD=" + "Ab1" * 400 + " next", f"PASSWORD={A} next"),
        ('PASSWORD="' + "Ab1" * 400 + '" next', f'PASSWORD="{A}" next'),
        ("postgres://u:Abcd1234'xyz@db/app", f"postgres://u:{A}@db/app"),
        (f"postgres://u:{MASKED}@db/app", f"postgres://u:{A}@db/app"),
        ("postgres://u:" + "Ab1" * 100 + "@db/app", f"postgres://u:{A}@db/app"),
        (
            "db:\n  password: |\n    Abcd1234Efgh5678\n    more\n  user: x\n",
            f"db:\n  password: |\n{A}\n  user: x\n",
        ),
    ],
)
def test_strong_forms_redact_whatever_the_value_looks_like(text, expected):
    assert redact(text) == expected


def test_a_long_dotted_run_is_scanned_in_linear_time():
    import time

    text = "a." * 200_000 + "="
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    "text, expected",
    [
        ('password="Abcd1234\nEfgh5678" next', f'password="{A}" next'),
        ("password='Abcd1234\nEfgh5678' next", f"password='{A}' next"),
        (f"PASSWORD=${{VAR}}{MASKED} next", f"PASSWORD={A} next"),
        (f"PASSWORD=[redacted assignment]{MASKED} next", f"PASSWORD={A} next"),
        (f"PASSWORD=$(printf x){MASKED} next", f"PASSWORD={A} next"),
        (f"PASSWORD=${{VAR}},{MASKED} next", f"PASSWORD={A} next"),
        (f"PASSWORD=[redacted assignment];{MASKED} next", f"PASSWORD={A} next"),
        (f"PASSWORD=$(printf x)){MASKED} next", f"PASSWORD={A} next"),
        ('{\\"password\\":\\"Abcd1234\\\\\\"Efgh5678\\"}', '{\\"password\\":\\"' + A + '\\"}'),
        ('{\\"password\\":\\"Abcd\\\\\\\\1234\\"}', '{\\"password\\":\\"' + A + '\\"}'),
        (f'PASSWORD="[{MASKED}]"', f'PASSWORD="{A}"'),
        ("postgres://o'connor:Abcd1234Efgh5678@db/app", f"postgres://o'connor:{A}@db/app"),
        (
            f'password="{MASKED} and the quote never closes',
            f'password="{A} and the quote never closes',
        ),
    ],
)
def test_round_two_review_cases(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "PASSWORD=${ " * 50_000,
        'password="x ' * 50_000,
        '\\"password\\":\\"x ' * 50_000,
        "PASSWORD=$(" * 50_000,
    ],
)
def test_adversarial_runs_of_keys_scan_in_linear_time(text):
    import time

    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 2.0


def test_references_and_masks_that_are_the_whole_value_are_kept():
    for text in (
        "PASSWORD=${DB_PASSWORD:-x} next",
        "PASSWORD=$(bw get password db) next",
        "PASSWORD=[REDACTED]",
        'PASSWORD="[redacted github sha256:0123abcd]"',
        "PASSWORD=<your key>",
    ):
        assert redact(text) is text, text
