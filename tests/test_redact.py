"""Secrets are replaced by a placeholder before any backend stores a drawer (#596)."""

from __future__ import annotations

import hashlib
import random as _random
import string as _string

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
    # A LiteLLM virtual key: "sk-" and token_urlsafe(16), 22 characters.
    "litellm": "sk-" + "Xq7" * 7 + "B",
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


# The security-guidance review of the round-4 commit (2026-11-07).
@pytest.mark.parametrize(
    "text, expected",
    [
        ("postgres://u:p@ss/w0rd@db/app", f"postgres://u:{A}@db/app"),
        (
            "smtp://jp@example.org:Abcd1234Efgh@mail.example.org:587",
            f"smtp://jp@example.org:{A}@mail.example.org:587",
        ),
        ("postgres://u:changeme@db/app", f"postgres://u:{A}@db/app"),
        ("PASSWORD=$ecr3tValue next", f"PASSWORD={A} next"),
        (f'PASSWORD="[redacted {MASKED}]" next', f'PASSWORD="{A}" next'),
        (f"PASSWORD=[redacted-{MASKED}] next", f"PASSWORD={A} next"),
        ("PASSWORD=abc'def123 next", f"PASSWORD={A} next"),
        (
            "PASSWORD=Abcd\\\\1234\\nnext",
            f"PASSWORD={A}\\nnext",
        ),  # raw JSONL: a literal backslash, then \n
        ("PASSWORD=Abcd\\x1234 next", f"PASSWORD={A} next"),
        (f"PASSWORD=${{VAR}}'{MASKED} next", f"PASSWORD={A} next"),
        ("PASSWORD=Abcd1234'\\xSecret next", f"PASSWORD={A} next"),
        ("PASSWORD=Abcd1234''Secret next", f"PASSWORD={A} next"),
        ("PASSWORD=${VAR}'\\xSecret next", f"PASSWORD={A} next"),
        ("postgres://u:pa\\ss1234@db/app", f"postgres://u:{A}@db/app"),
        ("postgres://u:pa\\\\ss1234@db/app", f"postgres://u:{A}@db/app"),  # raw JSONL backslash
        ("smb://CORP\\jp:Secret1234@files/share", f"smb://CORP\\jp:{A}@files/share"),
        ("env X='PASSWORD=Abcd1234' next", f"env X='PASSWORD={A}' next"),
    ],
)
def test_security_review_cases(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a://" + "\\" * 100_000 + "x",
        "a://b:" * 50_000,
        "a://b:c" * 50_000 + "@",
        "x://u:" + "p@" * 100_000,
    ],
)
def test_runs_of_url_heads_scan_in_linear_time(text):
    import time

    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 2.0


def test_url_references_and_quoted_urls_keep_their_shape():
    assert redact("postgres://palace:${PW}@db postgres://u:$DB_PASS@db") == (
        "postgres://palace:${PW}@db postgres://u:$DB_PASS@db"
    )
    assert redact(f"'postgresql://calcom:{BARE}@localhost:5432/calcom',\\n") == (
        f"'postgresql://calcom:{A}@localhost:5432/calcom',\\n"
    )


def test_a_google_oauth_client_secret_is_redacted_and_a_bare_prefix_is_not():
    value = "GOC" + "SPX-" + "Ab3_" * 7
    assert redact(f"client secret {value} copied") == (
        f"client secret {_placeholder('google', value)} copied"
    )
    text = 'search for "GOCSPX-" with grep -F'
    assert redact(text) is text


@pytest.mark.parametrize(
    "value",
    [
        "xo" + "xb-1234-5678-" + "abcd" * 6,
        "xa" + "pp-1-A0B1C2D3E4-5678-" + "ab12" * 16,
        "xo" + "xe-1-" + "Zz09" * 10,
        "xo" + "xe.xoxp-1-" + "Qq77" * 10,
    ],
)
def test_every_slack_token_class_is_redacted(value):
    assert redact(f"token {value} end") == f"token {_placeholder('slack', value)} end"


def test_slack_placeholders_in_docs_are_left_alone():
    text = "paste the xoxb-your-token or the xapp-... from the app page"
    assert redact(text) is text


# --- 2026-11-11: the shapes queued after the 11-07..11-09 leaks -------------------------------
GOCSPX = "GOC" + "SPX-"


@pytest.mark.parametrize("tail", [24, 28, 29, 32])
def test_a_google_client_secret_of_any_tail_from_24_is_redacted(tail):
    value = GOCSPX + ("Ab3_9" * 8)[:tail]
    assert redact(f"secret {value} end") == f"secret {_placeholder('google', value)} end"


def test_a_google_client_secret_prefix_with_a_short_tail_is_left():
    text = "see " + GOCSPX + "abc in the docs"
    assert redact(text) is text


def _jwt():
    return "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiI0MmMxIiwiaWF0IjoxfQ" + "." + "s1G" * 14


@pytest.mark.parametrize("prefix", ["HA_TOKEN ", "Bearer ", "\\n", "token="])
def test_a_bare_jwt_is_redacted_wherever_it_is_printed(prefix):
    value = _jwt()
    out = redact(f"{prefix}{value} end")
    assert value not in out
    assert "[redacted jwt sha256:" in out or "[redacted assignment]" in out


def test_a_base64_json_blob_is_redacted_and_a_short_one_is_not():
    value = "ey" + "JpZCI6" + "IkFiYzEyMyIs" * 9
    assert redact(f"echo {value} | bw edit item x") == (
        f"echo {_placeholder('blob', value)} | bw edit item x"
    )
    text = "cursor eyJwYWdlIjoyfQ next"
    assert redact(text) is text


def test_an_otpauth_uri_is_redacted_whole():
    value = "otp" + "auth://totp/Acme:jp?secret=" + "JBSWY3DPEHPK3PXP" + "&issuer=Acme"
    assert redact(f"uri {value} done") == f"uri {_placeholder('otpauth', value)} done"


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "G-482913 is your Google verification code.",
            "G-[redacted code] is your Google verification code.",
        ),
        (
            "\\nG-482913 is your Google verification code.",
            "\\nG-[redacted code] is your Google verification code.",
        ),
        ("482913 is your Acme sign-in code", "[redacted code] is your Acme sign-in code"),
        ("Your verification code is 482913.", "Your verification code is [redacted code]."),
        ("Verification code: 4829", "Verification code: [redacted code]"),
    ],
)
def test_a_one_time_code_beside_its_words_is_redacted_without_a_hash(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "build 482913 is your build number",
        "in 2026 we shipped 123456 items",
        "a [redacted code] stays as it is",
    ],
)
def test_digits_without_code_words_are_left(text):
    assert redact(text) is text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Support PIN: 4821 thanks", "Support PIN: [redacted assignment] thanks"),
        ('{"pin": "4821"}', '{"pin": "[redacted assignment]"}'),
        ("PIN_CODE=123456", "PIN_CODE=[redacted assignment]"),
        ("accountPasscode: 90210", "accountPasscode: [redacted assignment]"),
    ],
)
def test_a_pin_of_digits_is_redacted(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text", ["pin=numpy", "pin: true", '{"pin": "latest"}', "spin: 1234", "pinned: 1234"]
)
def test_a_pin_name_without_digits_or_a_word_merely_containing_pin_is_left(text):
    assert redact(text) is text


def _bw_item():
    return {
        "object": "item",
        "id": "0f0f",
        "name": "Registrar",
        "notes": "recovery 1234-5678 and the old pw",
        "fields": [
            {"name": "API Key", "value": "k3yv4lu3", "type": 0},
            {"name": "Color", "value": "blue", "type": 0},
            {"name": "Recovery", "value": "rc0d3s", "type": 1},
            {"name": "Support PIN", "value": "4821", "type": 0},
            {"name": "Unset", "value": None, "type": 1},
        ],
        "login": {"username": "jp", "password": "pw", "totp": "JBSWY3DPEHPK3PXP"},
    }


def _assert_bw_item_redacted(out):
    for secret in ("recovery 1234-5678", "k3yv4lu3", "rc0d3s", "4821", "JBSWY3DPEHPK3PXP"):
        assert secret not in out
    assert "blue" in out and "Registrar" in out and '"jp"' in out.replace('\\"', '"')


def test_a_vault_item_has_its_notes_secret_fields_and_totp_redacted():
    import json

    out = redact(json.dumps(_bw_item()))
    _assert_bw_item_redacted(out)
    assert '"value": null' in out


def test_a_vault_item_inside_a_raw_jsonl_line_is_redacted_too():
    import json

    out = redact(json.dumps({"type": "tool_result", "content": json.dumps(_bw_item())}))
    _assert_bw_item_redacted(out)


def test_notes_outside_a_vault_item_are_left():
    text = '{"notes": "meeting notes are fine", "object": "page"}'
    assert redact(text) is text


def test_a_field_named_as_nothing_secret_and_not_hidden_is_left():
    text = '[{"name": "Color", "value": "blue", "type": 0}]'
    assert redact(text) is text


@pytest.mark.parametrize(
    "text",
    [
        "eyJ-" * 40000,
        "eyJaaaaaaaa." * 40000,
        ("123456 is your " + "a" * 50 + " ") * 2000,
        '"name":"API Key","value":' * 5000,
        "otpauth://" * 40000,
        '\\"object\\":\\"item\\",\\"notes\\":' * 5000,
    ],
)
def test_the_new_shapes_scan_long_adversarial_runs_in_linear_time(text):
    import time

    start = time.monotonic()
    redact(text)
    assert time.monotonic() - start < 2.0


def test_a_second_pass_over_the_new_placeholders_changes_nothing():
    import json

    once = redact(json.dumps(_bw_item()) + " G-482913 is your Google verification code " + _jwt())
    assert redact(once) == once


# --- 2026-11-11 security review of #604 --------------------------------------------------------
def test_a_pretty_printed_vault_item_is_redacted_plain_and_in_a_jsonl_line():
    import json

    pretty = json.dumps(_bw_item(), indent=2)
    _assert_bw_item_redacted(redact(pretty))
    _assert_bw_item_redacted(redact(json.dumps({"type": "tool_result", "content": pretty})))


def test_a_hidden_field_with_type_before_value_is_redacted():
    text = '[{"name": "Recovery", "type": 1, "value": "rc0d3s"}]'
    assert redact(text) == '[{"name": "Recovery", "type": 1, "value": "[redacted assignment]"}]'
    shown = '[{"name": "Color", "type": 0, "value": "blue"}]'
    assert redact(shown) is shown


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PIN: 1234 5678 thanks", "PIN: [redacted assignment] thanks"),
        ('{"pin": "12-34-56"}', '{"pin": "[redacted assignment]"}'),
        ("pin=4821-9930", "pin=[redacted assignment]"),
    ],
)
def test_a_grouped_pin_is_redacted_whole(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        ("eyJaaaa" * 57 + ".eyJ" + "b" * 7990 + " ") * 40,
        '"name":' + "\\n " * 40000,
        '"name": "x"' + " ," * 40000,
        '\\"object\\":' + "\\t" * 40000,
    ],
)
def test_review_adversarial_runs_scan_in_linear_time(text):
    import time

    start = time.monotonic()
    redact(text)
    assert time.monotonic() - start < 2.0


# --- 2026-11-11 cross-model review of #604 (gpt-6.1-sol, round 1) -----------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Your sign-in code is 482913.", "Your sign-in code is [redacted code]."),
        ("Your security code is 482913.", "Your security code is [redacted code]."),
        ("Your verification code:482913", "Your verification code:[redacted code]"),
        ("482913 IS YOUR VERIFICATION CODE", "[redacted code] IS YOUR VERIFICATION CODE"),
        ("Login code = 4829", "Login code = [redacted code]"),
    ],
)
def test_one_time_codes_in_either_order_and_any_case(text, expected):
    assert redact(text) == expected


def test_a_jwt_whose_payload_is_not_compact_json_is_redacted():
    value = "ey" + "JhbGciOiJIUzI1NiJ9" + ".e30." + "s1G" * 14
    assert redact(f"token {value} end") == f"token {_placeholder('jwt', value)} end"


def test_a_pretty_json_blob_is_redacted_and_a_long_path_is_not():
    import base64
    import json

    blob = base64.b64encode(json.dumps(_bw_item(), indent=2).encode()).decode()
    assert blob.startswith("ew")
    assert redact(f"echo {blob} | bw edit item x") == (
        f"echo {_placeholder('blob', blob)} | bw edit item x"
    )
    path = "see /var/n" + "ews/" + "abcd/" * 30 + " end"
    assert redact(path) is path


@pytest.mark.parametrize(
    "obj",
    [
        '{"type": 1, "name": "Recovery", "value": "rc0d3s"}',
        '{"value": "rc0d3s", "name": "Recovery", "type": 1}',
        '{"value": "rc0d3s", "type": 0, "name": "Backup Password"}',
        '{\\"type\\": 1, \\"name\\": \\"Recovery\\", \\"value\\": \\"rc0d3s\\"}',
    ],
)
def test_a_field_is_judged_by_its_siblings_in_any_order(obj):
    assert "rc0d3s" not in redact(f"[{obj}]")


@pytest.mark.parametrize(
    "text",
    [
        '[{"type": 0, "value": "blue", "name": "Color"}]',
        '{"type": 1, "value": "x1"}',
        '{"value": "abc", "type": 1} and later {"name": "Password"}',
    ],
)
def test_a_value_without_a_secret_sibling_in_its_own_object_is_left(text):
    assert redact(text) is text


def test_notes_after_a_sorted_item_object_key_are_redacted():
    import json

    out = redact(json.dumps(_bw_item(), sort_keys=True))
    _assert_bw_item_redacted(out)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PIN: 4 8 2 1", "PIN: [redacted assignment]"),
        ("PIN: 4-8-2-1.", "PIN: [redacted assignment]."),
    ],
)
def test_a_pin_spelled_digit_by_digit_is_redacted(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("pin: 1234 5678 9012 3456 end", "pin: [redacted assignment] end"),
        ("pin=12345678901234", "pin=[redacted assignment]"),
    ],
)
def test_a_digit_run_longer_than_a_pin_is_replaced_whole(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "unit",
    [
        "eyJ-",
        "eyJaaaaaaaa.e",
        '{"value": "x", ',
        '"name": "a", "value": "b"} ',
        "is your " * 3 + "code ",
        "security code 1",
        "pin: 1 ",
    ],
)
def test_review_runs_scale_linearly(unit):
    import time

    def took(n):
        start = time.monotonic()
        redact(unit * n)
        return time.monotonic() - start

    small, big = took(5000), took(40000)
    # Eight times the input; a quadratic scan would take ~64x. Allow noise on tiny timings.
    assert big < 20 * small + 0.25


# --- 2026-11-11 cross-model review of #604 (round 2) -------------------------------------------
@pytest.mark.parametrize(
    "build",
    [
        lambda n: "security code" + " " * n + "x",
        lambda n: "security code is" + " " * n + ":" + " " * n + "x",
        lambda n: "482913" + " is your" + " " * n + "x",
        lambda n: '{"value": "v"' + " " * n + "}",
    ],
)
def test_long_whitespace_runs_with_a_failing_suffix_scale_linearly(build):
    import time

    def took(n):
        start = time.monotonic()
        redact(build(n))
        return time.monotonic() - start

    small, big = took(5000), took(40000)
    assert big < 20 * small + 0.25


def test_notes_brought_near_their_item_by_a_shortened_password_are_redacted_in_one_pass():
    text = '{"object":"item","password":"' + "a" * 5000 + '","notes":"recovery material"}'
    once = redact(text)
    assert "recovery material" not in once
    assert redact(once) == once


# --- 2026-11-11 automated security review of e90f8f26 ------------------------------------------
@pytest.mark.parametrize(
    "text, secret",
    [
        ('[{"name": "API {Key}", "value": "k3yv4l", "type": 0}]', "k3yv4l"),
        ('[{"name": "Recovery", "value": "rc0d3s", "note": "a}b", "type": 1}]', "rc0d3s"),
        ('{"name": "API Key", "value": "trunc4ted"', "trunc4ted"),
        ('{\\"name\\": \\"API {Key}\\", \\"value\\": \\"k3yv4l\\"}', "k3yv4l"),
    ],
)
def test_a_brace_in_a_string_or_a_missing_brace_does_not_hide_a_field(text, secret):
    assert secret not in redact(text)


def test_a_blob_after_a_slash_is_redacted():
    value = "ey" + "JpZCI6" + "IkFiYzEyMyIs" * 9
    assert value not in redact(f"GET /api/{value} 200")


def test_a_field_cut_from_its_braces_still_finds_a_name_after_its_value():
    assert "x9y8z7" not in redact('..."value": "x9y8z7", "name": "API Key", "type": 0')


@pytest.mark.parametrize(
    "unit",
    [
        '"value": "a", "s": "{{{{{{{{{{{{", ',
        '"value": "a", "s": "}}}}}}}}}}}}", ',
        '"s": "{", "value": "b", "t": "}", ',
    ],
)
def test_braces_inside_strings_near_many_values_scale_linearly(unit):
    import time

    def took(n):
        start = time.monotonic()
        redact(unit * n)
        return time.monotonic() - start

    small, big = took(1000), took(8000)
    assert big < 20 * small + 0.25


# --- 2026-11-11 cross-model review of #604 (round 3) and security review of db2ba399 ----------
def test_a_notes_cascade_past_the_pass_cap_fails_closed_and_is_a_fixed_point():
    text = '{"object":"item",' + ",".join('"notes":"' + "a" * 5000 + '"' for _ in range(10)) + "}"
    once = redact(text)
    assert "a" * 5000 not in once
    assert redact(once) == once


def test_a_field_brought_within_reach_by_shortened_siblings_is_redacted_in_one_call():
    tokens = ",".join('"token":"' + "b" * 1500 + '"' for _ in range(12))
    text = '{"name":"API Key",' + tokens + ',"value":"k3yv4l"}'
    once = redact(text)
    assert "k3yv4l" not in once
    assert redact(once) == once


def test_the_wide_pass_redacts_a_field_out_of_reach():
    cascade = (
        '{"object":"item",' + ",".join('"notes":"' + "a" * 5000 + '"' for _ in range(10)) + "}"
    )
    far = '{"name":"API Key",' + ",".join(f'"x{i}":"' + "c" * 200 + '"' for i in range(8))
    text = cascade + " " + far + ',"value":"k3yv4l"}'
    once = redact(text)
    assert "k3yv4l" not in once
    assert redact(once) == once


@pytest.mark.parametrize("escaped", [False, True])
def test_an_escaped_backslash_before_a_closing_quote_does_not_hide_a_field(escaped):
    import json

    obj = json.dumps({"name": "API Key", "note": "a" * 400 + "\\", "value": "k3yv4l", "type": 0})
    text = json.dumps({"content": obj}) if escaped else obj
    assert "k3yv4l" not in redact(text)


def test_any_secret_name_among_the_siblings_counts():
    text = '{"name": "Color", "label": "x", "name": "Password", "value": "s3cr3tv"}'
    assert "s3cr3tv" not in redact(text)


# --- 2026-11-11 cross-model review of #604 (round 4) -------------------------------------------
def test_the_wide_pass_finds_the_item_marker_once_not_per_notes_key():
    import time

    cascade = (
        '{"object":"item",' + ",".join('"notes":"' + "a" * 5000 + '"' for _ in range(10)) + "}"
    )

    def took(n):
        text = '{"notes":"x"},' * n + cascade
        start = time.monotonic()
        out = redact(text)
        assert "a" * 5000 not in out
        return time.monotonic() - start

    small, big = took(2000), took(16000)
    assert big < 20 * small + 0.25


@pytest.mark.parametrize("escaped", [False, True])
def test_an_odd_escaped_quote_and_a_brace_in_a_string_do_not_hide_a_field(escaped):
    import json

    # The name sits beyond the 300-character fallback, so only a correct quote count finds it.
    obj = json.dumps(
        {"name": "API Key", "pad": "p" * 400, "note": 'say " then {', "value": "k3yv4l"}
    )
    text = json.dumps({"content": obj}) if escaped else obj
    assert "k3yv4l" not in redact(text)


@pytest.mark.parametrize(
    "body",
    [
        "Xq7" * 7 + "B",  # alphanumeric, as the key leaked on 2026-11-14 was
        "aB3_" * 5 + "c-D",  # token_urlsafe may use "_" and "-"
        "Mk" + "9z" * 30,  # a longer master key
        "q4" * 11,  # lower case and digits only
        "Q4" * 11,  # upper case and digits only
        "aB3_" * 30 + "x",  # past 100 characters
    ],
)
@pytest.mark.parametrize("before", ["api_key ", "Bearer ", "\\n", '"', "key: ", "KEY="])
@pytest.mark.parametrize("after", [" end", "@proxy.example:4000/v1", "@openssh.com.evil/v1"])
def test_a_litellm_key_is_redacted_wherever_it_is_printed(body, before, after):
    value = "sk-" + body
    out = redact(f"{before}{value}{after}")
    assert value not in out
    assert body[:8] not in out


@pytest.mark.parametrize(
    "text",
    [
        "pip install sk-learn-compatible-estimators-for-x",  # one case only: a kebab name
        "the task-ManagementDashboardOverview page",  # "sk-" inside a word
        "SK-" + "Q7" * 12,  # not the lower-case prefix
        "sk-" + "Ab1" * 6,  # 18 characters: shorter than any key
        "sk-" + "abcdefghij" * 3,  # letters of one case: a name, not a key
        "PubkeyAcceptedAlgorithms sk-ecdsa-sha2-nistp256@openssh.com,"
        "sk-ssh-ed25519-cert-v01@openssh.com",  # SSH key types
    ],
)
def test_text_that_only_resembles_a_litellm_key_is_left(text):
    assert redact(text) is text


def test_a_provider_key_keeps_its_own_kind_before_the_litellm_shape():
    for kind in ("anthropic", "openai"):
        value = FAKE[kind]
        assert redact(value) == _placeholder(kind, value)


@pytest.mark.parametrize(
    "value",
    [
        "sk-" + "A1" * 20 + "_" + "b2C" * 10,
        "sk-" + "A" * 40 + "_",  # letters of one case: only the openai shape takes it
        "sk-" + "Ab1" * 334 + "_",  # past the litellm shape's 1000
    ],
)
def test_a_long_key_with_punctuation_is_redacted_to_the_end_of_its_token(value):
    # The bare openai shape once stopped at "_" and left the rest of the key.
    out = redact(f"key {value} end")
    assert out == f"key {_placeholder('openai', value)} end"


def test_a_key_inside_a_longer_word_is_still_redacted():
    value = "sk-" + "Ab1" * 20 + "_"
    out = redact(f"prefix-{value} end")
    assert value not in out and "Ab1Ab1" not in out


@pytest.mark.parametrize(
    "text",
    [
        " sk-" * 20000,
        (" sk-" + "aB3-" * 400) * 40,
        "sk-" + "a" * 100000,
        ("sk-" + "A" * 999 + "_") * 60,
    ],
)
def test_adversarial_sk_runs_scan_in_linear_time(text):
    import time

    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 2.0


# --- #619 slice 1: shapes without a vendor prefix, and vault-item needles -------------------------
# Synthetic values, built at run time (the issue's recipe); nothing here is a real credential.
SHAPE_TOKEN = "".join(_random.Random(619).sample(_string.ascii_letters + _string.digits, 32))
assert (
    any(c.isupper() for c in SHAPE_TOKEN)
    and any(c.islower() for c in SHAPE_TOKEN)
    and any(c.isdigit() for c in SHAPE_TOKEN)
)


def _ph(kind, value):
    return f"[redacted {kind} sha256:{hashlib.sha256(value.encode()).hexdigest()[:8]}]"


@pytest.mark.parametrize(
    "template",
    [
        "why: the key {t} looks live",  # a judge's free-text why
        "token={t}",
        "bare on a line\n{t}\nnext",
        '{{"type":"text","text":"x\\n{t}"}}',  # raw JSONL: right after a literal \n escape
    ],
)
def test_619_bare_mixed_token_is_redacted(template):
    out = redact(template.format(t=SHAPE_TOKEN))
    assert SHAPE_TOKEN not in out
    assert "[redacted" in out


def test_619_hex_runs_of_32_or_more_but_not_a_git_sha():
    rnd = _random.Random(32)
    h32 = "".join(rnd.choice("0123456789abcdef") for _ in range(32))
    h64 = "".join(rnd.choice("0123456789abcdef") for _ in range(64))
    sha1 = "".join(rnd.choice("0123456789abcdef") for _ in range(40))
    out = redact(f"k {h32} and {h64} and commit {sha1}")
    assert h32 not in out and h64 not in out
    assert _ph("hex", h32) in out and _ph("hex", h64) in out
    assert sha1 in out  # exactly 40 hex: a git sha, kept for recall


def test_619_quoted_secret_alphabet_is_redacted_but_paths_urls_times_are_not():
    q = "Ab3dEf+hJkLmNoP/q9"  # 18 chars, base64 alphabet, 3 classes
    out = redact(f'cfg "{q}" end')
    assert q not in out
    kept = [
        '"/home/jp/Projects/memorypalace/mempalace"',
        '"https://example.org/a/b/c/d/e"',
        '"2026-11-15T13:21:02.179Z"',
        '"mempalace.backends.postgres"',
        '"2026-11-12-plan-design.md"',
        '"just_a_long_snake_case_name"',
    ]
    for s in kept:
        assert redact(f"x {s} y") == f"x {s} y", s


def test_619_url_encoded_dsn_password_is_redacted():
    pw = "Ab%2Bcd%2F" + "1234xyzQ" * 2
    out = redact(f"DSN postgresql://palace:{pw}@localhost:5433/db")
    assert pw not in out and "Ab%2B" not in out


@pytest.mark.parametrize(
    "plain",
    [
        "drawer_palace_nemotron_references_d6daffa975cc887fcae92963",
        "see 2026-11-12-plan-design.md and /home/jp/Projects/memorypalace/tests",
        "SecretPatternsUnavailableErrorHandler raised",  # CamelCase, no digit
        "uuid 2ca17ab7-9efe-4c37-bc71-2a23568bc11c here",
        "Ordinary prose stays exactly as it was written, with no tokens in it.",
    ],
)
def test_619_negative_controls_unchanged(plain):
    assert redact(plain) == plain


def test_619_idempotent_on_new_kinds():
    rnd = _random.Random(7)
    hexrun = "".join(rnd.choice("0123456789abcdef") for _ in range(32))
    once = redact(f"a {SHAPE_TOKEN} b {hexrun}")
    assert SHAPE_TOKEN not in once and hexrun not in once
    assert redact(once) == once


# The vault needle: config names vault item ids; the values are fetched in process, never on disk.
VAULT_VALUE = "hunt" + "erpass"  # lower case only: no shape rule can catch it


@pytest.fixture
def _vault(_config, monkeypatch):
    import json as _json

    import mempalace.redact as R

    (_config / "config.json").write_text(
        _json.dumps({"secret_vault_items": [{"id": "item-0001", "label": "example-shared"}]})
    )
    calls = []

    def fake_fetch(item_id):
        calls.append(item_id)
        return VAULT_VALUE

    monkeypatch.setattr(R, "_vault_fetch", fake_fetch)
    R._cache["key"] = None
    return calls


def test_619_vault_needle_redacts_the_value_on_word_boundaries(_vault, _config):
    out = redact(f"wifi pw {VAULT_VALUE}, and {VAULT_VALUE}1 and x{VAULT_VALUE}x")
    assert out.startswith("wifi pw [redacted vault example-shared],")
    assert f"x{VAULT_VALUE}x" in out  # inside a longer word: not this value
    assert _vault == ["item-0001"]
    assert VAULT_VALUE not in (_config / "config.json").read_text()


def test_619_vault_needle_fails_closed(_config, monkeypatch):
    import mempalace.redact as R

    (_config / "config.json").write_text(
        '{"secret_vault_items": [{"id": "item-0001", "label": "x"}]}'
    )

    def broken(item_id):
        raise SecretPatternsUnavailable("vault locked")

    monkeypatch.setattr(R, "_vault_fetch", broken)
    R._cache["key"] = None
    with pytest.raises(SecretPatternsUnavailable):
        redact("anything")


@pytest.mark.parametrize(
    "body",
    [
        '{"secret_vault_items": "x"}',
        '{"secret_vault_items": [{"id": "a"}]}',
        '{"secret_vault_items": [{"id": "a", "label": "Bad Label!"}]}',
    ],
)
def test_619_vault_items_config_must_be_well_formed(_config, body):
    import mempalace.redact as R

    (_config / "config.json").write_text(body)
    R._cache["key"] = None
    with pytest.raises(SecretPatternsUnavailable):
        redact("anything")


def test_619_long_runs_are_redacted_whole_and_fast():
    import time as _time

    rnd = _random.Random(5000)
    alpha = _string.ascii_letters + _string.digits
    long_tok = "".join(rnd.choice(alpha) for _ in range(5000))
    long_hex = "".join(rnd.choice("0123456789abcdef") for _ in range(3000))
    out = redact(f"a {long_tok} b {long_hex} c")
    assert long_tok[:50] not in out and long_tok[-50:] not in out
    assert long_hex[:50] not in out
    blob = ("word " * 40000) + "x" * 100000  # 300 KB of prose and one long single-class run
    t0 = _time.monotonic()
    redact(blob)
    assert _time.monotonic() - t0 < 5


def test_619_a_git_sha_in_any_case_is_kept():
    rnd = _random.Random(40)
    sha = "".join(rnd.choice("0123456789abcdefABCDEF") for _ in range(40))
    assert redact(f"commit {sha} and '{sha}'") == f"commit {sha} and '{sha}'"


def test_619_vault_value_whitespace_is_kept_exactly(_config, monkeypatch):
    import json as _json

    import mempalace.redact as R

    (_config / "config.json").write_text(
        _json.dumps({"secret_vault_items": [{"id": "i", "label": "sp"}]})
    )
    monkeypatch.setattr(R, "_vault_fetch", lambda _i: " " + VAULT_VALUE + " ")
    R._cache["key"] = None
    out = redact(f"pw: {VAULT_VALUE} done")  # the stored value has spaces round it
    assert "[redacted vault sp]" in out and VAULT_VALUE not in out


def test_619_vault_fetch_keeps_inner_and_edge_spaces(monkeypatch):
    import subprocess as _sp

    import mempalace.redact as R

    class Done:
        returncode, stdout = 0, b"  two words  \n"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: Done())
    assert R._vault_fetch("i") == "  two words  "


def test_619_vault_values_refresh_after_the_ttl_and_fail_closed(_config, monkeypatch):
    import json as _json

    import mempalace.redact as R

    (_config / "config.json").write_text(
        _json.dumps({"secret_vault_items": [{"id": "i", "label": "rot"}]})
    )
    state = {"value": VAULT_VALUE, "now": 1000.0}
    monkeypatch.setattr(R, "_vault_fetch", lambda _i: state["value"])
    monkeypatch.setattr(R.time, "monotonic", lambda: state["now"])
    R._cache["key"] = None
    assert "[redacted vault rot]" in redact(f"a {VAULT_VALUE} b")
    rotated = "rotat" + "edpass"
    state["value"] = rotated
    state["now"] += R._VAULT_TTL + 1  # no config change: only the TTL
    assert "[redacted vault rot]" in redact(f"a {rotated} b")

    def locked(_i):
        raise SecretPatternsUnavailable("vault answered rc 1, empty")

    monkeypatch.setattr(R, "_vault_fetch", locked)
    state["now"] += R._VAULT_TTL + 1
    with pytest.raises(SecretPatternsUnavailable):
        redact("anything")
    with pytest.raises(
        SecretPatternsUnavailable
    ):  # nothing stale stays cached after a failed refresh
        redact("anything")


def test_619_vault_needle_in_raw_jsonl_and_json_escaped(_config, monkeypatch):
    import json as _json

    import mempalace.redact as R

    (_config / "config.json").write_text(
        _json.dumps({"secret_vault_items": [{"id": "i", "label": "js"}]})
    )
    tricky = 'hunt"er\\pass'  # a quote and a backslash: JSON escapes them
    monkeypatch.setattr(R, "_vault_fetch", lambda _i: tricky)
    R._cache["key"] = None
    raw_jsonl = '{"text": "line one\\n' + VAULT_VALUE + '"}'
    for text in (
        "plain " + tricky + " end",
        _json.dumps({"text": "pw " + tricky}),  # JSON-escaped once
        _json.dumps({"line": _json.dumps({"text": "pw " + tricky})}),  # twice
    ):
        out = redact(text)
        assert "[redacted vault js]" in out, text
        assert "hunt" not in out
    monkeypatch.setattr(R, "_vault_fetch", lambda _i: VAULT_VALUE)
    R._cache["key"] = None
    assert VAULT_VALUE not in redact(raw_jsonl)  # right after a literal \n escape


def test_619_vault_value_with_too_short_a_core_fails_closed(_config, monkeypatch):
    import json as _json

    import mempalace.redact as R

    (_config / "config.json").write_text(
        _json.dumps({"secret_vault_items": [{"id": "i", "label": "pad"}]})
    )
    monkeypatch.setattr(R, "_vault_fetch", lambda _i: "   abc   ")
    R._cache["key"] = None
    with pytest.raises(SecretPatternsUnavailable):
        redact("abc")


def test_619_vault_fetch_keeps_carriage_returns_through_a_real_subprocess(tmp_path, monkeypatch):
    import mempalace.redact as R

    fake = tmp_path / "bw"
    fake.write_text("#!/bin/sh\nprintf 'alpha\\rbeta\\n'\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert R._vault_fetch("i") == "alpha\rbeta"


def test_619_vault_needle_matches_utf8_kept_json(_config, monkeypatch):
    import json as _json

    import mempalace.redact as R

    (_config / "config.json").write_text(
        _json.dumps({"secret_vault_items": [{"id": "i", "label": "u8"}]})
    )
    value = "\u00e9" + '"passw'
    monkeypatch.setattr(R, "_vault_fetch", lambda _i: value)
    R._cache["key"] = None
    for text in (
        _json.dumps({"text": value}, ensure_ascii=False),
        _json.dumps({"text": value}),
        _json.dumps({"line": _json.dumps({"text": value}, ensure_ascii=False)}, ensure_ascii=False),
    ):
        out = redact(text)
        assert "[redacted vault u8]" in out and "passw" not in out, text


def test_619_an_all_digit_run_is_a_number_not_hex():
    digits = "0123456789" * 6
    assert redact(f"n {digits} m") == f"n {digits} m"


def test_619_metadata_hash_and_id_fields_keep_their_value_but_text_fields_do_not():
    rnd = _random.Random(64)
    h = "".join(rnd.choice("0123456789abcdef") for _ in range(64))
    meta = {
        "content_hash": h,
        "source_dir_ino": h,
        "drawer_id": SHAPE_TOKEN,
        "aaak_source": f"cfg {SHAPE_TOKEN} and {h}",
        "entities": [SHAPE_TOKEN],
    }
    out = redact_metadatas(meta)
    assert out["content_hash"] == h and out["source_dir_ino"] == h
    assert (
        SHAPE_TOKEN not in out["drawer_id"]
    )  # a token-shaped value under an id key is still redacted
    assert SHAPE_TOKEN not in out["aaak_source"] and h not in out["aaak_source"]
    assert SHAPE_TOKEN not in out["entities"][0]  # an extracted entity is not an id field


def test_619_the_id_field_exemption_is_narrow():
    out = redact_metadatas(
        {
            "hashtag_notes": SHAPE_TOKEN,  # "hash" inside a word, not a suffix
            "content_hash": {"nested": SHAPE_TOKEN},  # nested under an id-named key
            "drawer_id": f"free text {SHAPE_TOKEN}",  # whitespace: free text, not an id
            "row_ids": [SHAPE_TOKEN],  # a list under an id-named key
        }
    )
    for k in out:
        assert SHAPE_TOKEN not in str(out[k]), k


def test_619_the_exemption_never_reaches_nested_values_or_lookalike_keys():
    rnd = _random.Random(65)
    h = "".join(rnd.choice("0123456789abcdef") for _ in range(64))
    out = redact_metadatas(
        {
            "recipe": {"aaak_source": SHAPE_TOKEN, "entities": [SHAPE_TOKEN], "inner_hash": h},
            "shadow": SHAPE_TOKEN,
            "hashtags": h,
            "drawer_id": SHAPE_TOKEN,
        }
    )
    s = str(out)
    assert SHAPE_TOKEN not in s
    assert out["hashtags"] != h  # "hashtags" is not a digest field
    assert out["recipe"]["inner_hash"] == h  # a whole-hex value under a digest key, at any depth


def test_619_a_list_of_digests_under_a_digest_key_is_kept():
    rnd = _random.Random(66)
    hs = ",".join("".join(rnd.choice("0123456789abcdef") for _ in range(64)) for _ in range(3))
    assert redact_metadatas({"content_hash": hs})["content_hash"] == hs
    assert (
        redact_metadatas({"content_hash": hs + "," + SHAPE_TOKEN})["content_hash"]
        != hs + "," + SHAPE_TOKEN
    )


def test_619_a_key_with_a_trailing_newline_is_not_a_digest_field():
    h = "0123456789abcdef" * 4
    assert redact_metadatas({"content_hash\n": h})["content_hash\n"] != h


# --- PEM private keys (2026-11-17: 4 distinct OpenSSH private keys found in drawers; no rule took them)

_HEAD = "-----" + "BEGIN OPENSSH " + "PRIVATE KEY-----"
_END = "-----" + "END OPENSSH " + "PRIVATE KEY-----"


def _pem_lines(seed=7, n=6):
    rnd = _random.Random(seed)
    alpha = _string.ascii_letters + _string.digits + "+/"
    return ["".join(rnd.choice(alpha) for _ in range(70)) for _ in range(n)]


@pytest.mark.parametrize(
    "sep",
    ["\n", "\\n", "\\\\n", "\r\n", "\\r\\n", "\\\\r\\\\n"],
    ids=["newlines", "json-escaped", "json-escaped-twice", "crlf", "crlf-json", "crlf-json-twice"],
)
def test_a_pem_private_key_block_becomes_one_placeholder(sep):
    lines = _pem_lines()
    block = sep.join([_HEAD, *lines, _END])
    out = redact(f"cat key{sep}{block}{sep}next")
    assert all(ln not in out for ln in lines) and "BEGIN OPENSSH" not in out
    assert out.startswith(f"cat key{sep}[redacted pem sha256:") and out.endswith(f"]{sep}next")
    assert redact(out) == out


def test_a_pem_key_without_its_end_line_is_still_taken_and_the_prose_after_it_stays():
    lines = _pem_lines(seed=8)
    prose = "Please keep this paragraph intact\nand this second line too"
    out = redact("\n".join([_HEAD, *lines]) + "\n" + prose)
    assert all(ln[:20] not in out for ln in lines)
    assert out.startswith("[redacted pem sha256:") and out.endswith("\n" + prose)


def test_a_traditional_encrypted_key_is_taken_with_its_preamble():
    lines = _pem_lines(seed=9, n=4)
    head = "-----" + "BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----" + "END RSA " + "PRIVATE KEY-----"
    block = "\n".join(
        [head, "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-256-CBC,0123ABCD", "", *lines, end]
    )
    out = redact(block + "\nafter")
    assert all(ln not in out for ln in lines) and "DEK-Info" not in out
    assert out.startswith("[redacted pem sha256:") and out.endswith("]\nafter")


def test_pkcs8_encrypted_and_narrow_wrapping_are_taken():
    head = "-----" + "BEGIN ENCRYPTED " + "PRIVATE KEY-----"
    end = "-----" + "END ENCRYPTED " + "PRIVATE KEY-----"
    rnd = _random.Random(10)
    alpha = _string.ascii_letters + _string.digits + "+/"
    lines = ["".join(rnd.choice(alpha) for _ in range(32)) for _ in range(8)]  # wrapped at 32
    out = redact("\n".join([head, *lines, end]))
    assert all(ln not in out for ln in lines)
    assert out.startswith("[redacted pem sha256:") and out.endswith("]")


def test_a_provider_shaped_run_inside_a_key_body_is_part_of_the_key():
    lines = _pem_lines(seed=11)
    lines[2] = FAKE["aws"] + lines[2][: 70 - len(FAKE["aws"])]
    out = redact("\n".join([_HEAD, *lines, _END]))
    assert "[redacted aws" not in out and FAKE["aws"] not in out
    assert out.startswith("[redacted pem sha256:") and out.count("[redacted") == 1


def test_a_mention_of_the_header_alone_is_left_as_it_is():
    text = f"The file starts with {_HEAD} and then the key body."
    assert redact(text) == text


def test_a_line_numbered_view_of_a_key_is_taken():
    lines = _pem_lines(seed=12)
    rows = [_HEAD, *lines, _END]
    for fmt in ("{n:>6}\t{row}", "{n:>6}→{row}"):
        text = "\n".join(fmt.format(n=n, row=row) for n, row in enumerate(rows, 1))
        out = redact(text)
        assert all(ln not in out for ln in lines), fmt


def test_a_pgp_private_key_block_is_taken():
    lines = _pem_lines(seed=13, n=5)
    head = "-----" + "BEGIN PGP " + "PRIVATE KEY BLOCK-----"
    end = "-----" + "END PGP " + "PRIVATE KEY BLOCK-----"
    out = redact("\n".join([head, "Version: GnuPG v2", "", *lines, "=AbCd", end]))
    assert all(ln not in out for ln in lines) and out.startswith("[redacted pem sha256:")


def test_a_putty_key_files_private_lines_are_taken_and_its_public_lines_stay():
    pub = _pem_lines(seed=14, n=2)
    priv = _pem_lines(seed=15, n=1)
    text = "\n".join(
        [
            "PuTTY-User-" + "Key-File-3: ssh-ed25519",
            "Encryption: none",
            "Comment: k",
            "Public-Lines: 2",
            *pub,
            "Private-Lines: 1",
            *priv,
            "Private-MAC: 00ff",
        ]
    )
    out = redact(text)
    assert priv[0] not in out and "[redacted ppk sha256:" in out
    assert "Private-MAC: 00ff" in out and "Encryption: none" in out


def test_a_key_shown_on_one_line_and_elided_is_taken():
    lines = _pem_lines(seed=16, n=1)
    for tail in ("...", "…"):
        out = redact(f"{_HEAD} {lines[0][:52]}{tail} rest of the line")
        assert lines[0][:52] not in out and out.endswith(f"]{tail} rest of the line")


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\\n", "\\\\n"], ids=["lf", "crlf", "json", "json2"])
def test_trailing_blanks_on_key_lines_do_not_stop_the_key(sep):
    lines = _pem_lines(seed=17)
    rows = [_HEAD, *(ln + (" " if k % 2 else "\t") for k, ln in enumerate(lines)), _END]
    out = redact(sep.join(rows) + sep + "after")
    assert all(ln not in out for ln in lines) and out.endswith(f"]{sep}after")


def test_header_lines_with_a_backslash_or_a_long_value_go_with_the_key():
    lines = _pem_lines(seed=18, n=3)
    head = "-----" + "BEGIN PGP " + "PRIVATE KEY BLOCK-----"
    end = "-----" + "END PGP " + "PRIVATE KEY BLOCK-----"
    text = "\n".join([head, "Comment: C:\\keys\\mine", "Comment: " + "x" * 300, "", *lines, end])
    out = redact(text)
    assert all(ln not in out for ln in lines) and out.startswith("[redacted pem sha256:")


def test_short_prose_lines_after_a_truncated_key_stay():
    lines = _pem_lines(seed=19)
    out = redact("\n".join([_HEAD, *lines, "Thanks", "Alice"]))
    assert all(ln[:20] not in out for ln in lines) and out.endswith("]\nThanks\nAlice")


def test_a_putty_private_section_stops_before_prose():
    priv = _pem_lines(seed=20, n=2)
    text = "\n".join(["Private-Lines: 2", *priv, "Thanks", "Alice"])
    out = redact(text)
    assert all(p not in out for p in priv) and out.endswith("]\nThanks\nAlice")


def test_a_pgp_short_last_data_line_before_its_checksum_is_taken():
    head = "-----" + "BEGIN PGP " + "PRIVATE KEY BLOCK-----"
    end = "-----" + "END PGP " + "PRIVATE KEY BLOCK-----"
    full = _pem_lines(seed=21, n=2)
    out = redact("\n".join([head, "", *full, "QUJD", "=AbCd", end]))
    assert all(ln not in out for ln in full) and "QUJD" not in out
    assert out.startswith("[redacted pem sha256:") and out.endswith("]")


@pytest.mark.parametrize(
    "tail",
    [" \n" * 5000 + "!", "\t \n" * 5000 + "!", " \\n" * 5000 + "!", "  7\t \n" * 5000 + "!"],
    ids=["blank-lines", "tab-blank-lines", "escaped-blank-lines", "numbered-blank-lines"],
)
@pytest.mark.parametrize("where", ["after-header", "after-a-body-line"])
def test_runs_of_blank_lines_cannot_stall_the_matcher(tail, where):
    # A subprocess with a hard limit: an exponential split would never return. Each rule gets its
    # own input, so the tail really is walked by both.
    import subprocess
    import sys

    body = "\n" + "A" * 64 + "\n" if where == "after-a-body-line" else "\n"
    code = (
        "import re, sys; from mempalace.redact import _PEM, _PPK; "
        "t = sys.stdin.read(); a, b = t.split('\\x00'); "
        "re.search(_PEM, a); re.search(_PPK, b)"
    )
    pem_text = _HEAD + body + tail
    ppk_text = "Private-Lines: 3" + body + tail
    r = subprocess.run(
        [sys.executable, "-c", code], input=pem_text + "\x00" + ppk_text, text=True, timeout=10
    )
    assert r.returncode == 0


@pytest.mark.parametrize("nl", ["\n", "\\n"], ids=["raw", "json"])
@pytest.mark.parametrize("blank", [" ", "\t", " \t "], ids=["space", "tab", "mixed"])
def test_blanks_after_the_begin_and_count_lines_do_not_stop_the_key(nl, blank):
    line = "Ab+/" * 16
    out = redact(f"{_HEAD}{blank}{nl}{line}{nl}{_END}")
    assert line not in out and out.startswith("[redacted pem sha256:")
    out = redact(f"Private-Lines: 1{blank}{nl}{line}{nl}Private-MAC: 00ff")
    assert line not in out and out.endswith(f"]{nl}Private-MAC: 00ff")


@pytest.mark.parametrize("nl", ["\n", "\\n"], ids=["raw", "json"])
@pytest.mark.parametrize("blank", [" ", "\t", " \t "], ids=["space", "tab", "mixed"])
def test_blanks_after_begin_then_header_lines_still_take_the_key(nl, blank):
    head = "-----" + "BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----" + "END RSA " + "PRIVATE KEY-----"
    line = "Ab+/" * 16
    text = nl.join(
        [head + blank, "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-256-CBC,0123ABCD", "", line, end]
    )
    out = redact(text)
    assert line not in out and "DEK-Info" not in out and out.startswith("[redacted pem sha256:")


@pytest.mark.parametrize("width", [128, 2000], ids=["128-columns", "unwrapped"])
def test_wide_and_unwrapped_key_bodies_are_taken(width):
    rnd = _random.Random(22)
    alpha = _string.ascii_letters + _string.digits + "+/"
    data = "".join(rnd.choice(alpha) for _ in range(2000))
    lines = [data[i : i + width] for i in range(0, len(data), width)]
    out = redact("\n".join([_HEAD, *lines, _END]) + "\nafter")
    assert all(ln[:40] not in out for ln in lines) and out.endswith("]\nafter")


@pytest.mark.parametrize("nl", ["\n", "\\n"], ids=["raw", "json"])
def test_a_header_line_of_any_length_does_not_hide_the_key(nl):
    head = "-----" + "BEGIN PGP " + "PRIVATE KEY BLOCK-----"
    end = "-----" + "END PGP " + "PRIVATE KEY BLOCK-----"
    lines = _pem_lines(seed=23, n=3)
    out = redact(nl.join([head, "Comment: " + "x" * 5000, "", *lines, end]))
    assert all(ln not in out for ln in lines) and out.startswith("[redacted pem sha256:")
