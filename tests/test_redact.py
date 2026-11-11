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


@pytest.mark.parametrize("text", ["pin: 1234 5678 9012 3456", "pin=12345678901234"])
def test_a_digit_run_longer_than_a_pin_is_left_whole(text):
    assert redact(text) is text


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
