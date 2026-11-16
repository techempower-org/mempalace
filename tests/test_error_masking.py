"""Endpoint errors never carry a credential (port of upstream 857fbec4, hardened in the 2026-11-15 review): an
error names the masked endpoint and describes the failure in fixed words, never repeating the reply, the HTTP
reason or an exception's text, and no original exception reaches a traceback. Fixture values are built at
runtime so no literal here looks like a credential."""

import io
import traceback
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from mempalace.embedding import EmbeddingAPIError, OpenAICompatEmbeddingFunction
from mempalace.llm_client import LLMError, _http_post_json, redact_secrets

V = "".join(("q7", "Zt", "Pk", "Lm", "2x", "Rv", "9w"))  # a value with no recognisable shape
SK = "-".join(("sk", "abcDEF123456" * 3))


def _llm_error(err=None, url="https://llm.example/v1"):
    ctx = (
        patch("mempalace.llm_client.urlopen", side_effect=err)
        if err
        else patch("mempalace.llm_client.urlopen")
    )
    with ctx:
        with pytest.raises(LLMError) as ei:
            _http_post_json(url, {}, {}, 5)
    return ei.value


def _embed_error(err=None, url="https://emb.example/v1"):
    ef = OpenAICompatEmbeddingFunction(url, "m")
    ctx = (
        patch("urllib.request.urlopen", side_effect=err) if err else patch("urllib.request.urlopen")
    )
    with ctx:
        with pytest.raises(EmbeddingAPIError) as ei:
            ef(["text"])
    return ei.value


def _shown(e):
    return str(e) + "".join(traceback.format_exception(e))


@pytest.mark.parametrize("code", [400, 401, 403, 500])
def test_no_http_error_repeats_the_reply_or_reason(code):
    def err():
        return HTTPError(
            "https://x.example", code, f"Invalid key {V}", {}, io.BytesIO(f"bad key {V}".encode())
        )

    for e in (_llm_error(err()), _embed_error(err())):
        assert V not in _shown(e), _shown(e)[-200:]
    assert _llm_error(err()).status == code
    if code in (401, 403):
        assert "authentication failed" in str(_llm_error(err()))


def test_connection_errors_name_the_kind_not_the_text():
    for err in (URLError(f"refused https://u:{V}@x"), OSError(111, "Connection refused")):
        for e in (_llm_error(err), _embed_error(err)):
            s = _shown(e)
            assert V not in s and e.__cause__ is None and e.__suppress_context__


def test_a_real_bad_url_is_refused_without_its_text():
    # no mock: Request itself raises on a URL without a scheme, with the URL in its message
    e = _llm_error(url=f"llm.example/?token={V}")
    assert "invalid URL" in str(e) and V not in _shown(e)
    ef = OpenAICompatEmbeddingFunction(f"emb.example/?token={V}", "m")
    with pytest.raises(EmbeddingAPIError) as ei:
        ef(["text"])
    assert V not in _shown(ei.value)


def test_redact_secrets_masks_urls_and_our_own_messages():
    for text in (
        f"key {SK}",
        f"Authorization: Bearer {V}",
        f"Authorization: Basic {V}==",
        f"https://user:{V}@llm.example/v1",
        f"https://llm.example/v1?api_key={V}&x=1",
        "{" + f'"password": "{V}"' + "}",
    ):
        out = redact_secrets(text)
        assert V not in out and SK not in out, (text, out)
    assert V not in str(LLMError(f"Cannot reach https://u:{V}@x"))
    assert V not in str(EmbeddingAPIError(f"request to https://x/?token={V} failed"))


def test_a_planted_strerror_is_never_copied():
    # an OSError's strerror is stored text, not the OS's words: only os.strerror(errno) may appear
    err = URLError(OSError(111, f"Invalid key {V}"))
    for e in (_llm_error(err), _embed_error(err)):
        assert V not in _shown(e)
        assert "Connection refused" in str(e)  # os.strerror(111)


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:opaque {V}@llm.example/v1",
        f"https://user:first@{V}@llm.example/v1",
        f"https://llm.example/v1?%74oken={V}",
        f"https://llm.example/v1#{V}",
        f"https://u:{V}@llm.example/v1",
    ],
)
def test_no_url_userinfo_query_or_fragment_reaches_an_error(url):
    err = HTTPError("https://x.example", 500, "boom", {}, io.BytesIO(b"{}"))
    for e in (_llm_error(err, url=url), _embed_error(err, url=url)):
        assert V not in _shown(e), str(e)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize(
    "reply",
    [
        {
            "data": [{"index": 0, "embedding": [V]}]
        },  # numpy: could not convert string to float: '<V>'
        {"data": [V]},  # a non-object row
        {"data": [{"index": 0}], "note": V},  # no "embedding" key
    ],
)
def test_a_malformed_embedding_reply_is_never_quoted(reply):
    import json

    ef = OpenAICompatEmbeddingFunction("https://emb.example/v1", "m")
    with patch("urllib.request.urlopen", return_value=_Resp(json.dumps(reply).encode())):
        with pytest.raises(EmbeddingAPIError) as ei:
            ef(["text"])
    assert V not in _shown(ei.value) and ei.value.__cause__ is None


def test_the_external_embedding_warning_never_logs_url_credentials(caplog):
    import logging

    import mempalace.embedding as emb

    emb._external_embedding_warned = False
    with caplog.at_level(logging.WARNING, logger="mempalace"):
        emb._warn_if_external_embedding(f"https://u:{V}@emb.example.com/v1?token={V}")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "outside the local network" in logged and V not in logged
