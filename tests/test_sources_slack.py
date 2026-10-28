"""Slack adapter: configured channels, one transcript per channel per day, read-only.

A fake Slack client stands in for the API. The ``require`` filter keeps only
matching messages from a channel (a brief in a self-DM, nothing else), and a
chunk that carries a sensitive marker is labelled so core files it privately.
"""

import pytest

from mempalace.sources import AuthRequiredError, SourceRef, get_adapter
from mempalace.sources.slack import SlackSourceAdapter

T0 = 1_761_580_800.0  # 2025-10-27 local-ish; only the day split matters


def _fake(history, replies=None, calls=None):
    replies = replies or {}

    def call(method, params):
        if calls is not None:
            calls.append(method)
        if method == "auth.test":
            return {"ok": True, "team": "Homefront"}
        if method == "users.list":
            return {
                "ok": True,
                "members": [{"id": "U1", "real_name": "Ada"}, {"id": "U2", "name": "bo"}],
            }
        if method == "conversations.list":
            return {"ok": True, "channels": [{"name": "events", "id": "C1"}]}
        if method == "conversations.history":
            return {"ok": True, "messages": history[params["channel"]]}
        if method == "conversations.replies":
            return {"ok": True, "messages": replies[params["ts"]]}
        raise AssertionError(f"unexpected Slack method {method}")

    return call


def _ingest(adapter):
    return list(adapter.ingest(source=SourceRef(local_path=None), palace=None))


def test_registered():
    assert get_adapter("slack").name == "slack"


def test_days_threads_mentions_and_only_read_methods(monkeypatch):
    monkeypatch.setattr("mempalace.sources.slack.configured_markers", lambda: ())
    calls = []
    history = {
        "C1": [
            {
                "ts": str(T0 + 60),
                "user": "U1",
                "text": "Potluck at <@U2>'s on Friday",
                "reply_count": 1,
            },
            {"ts": str(T0 + 120), "user": "U2", "text": "I'll bring soup &amp; bread"},
            {
                "ts": str(T0 + 86400 * 2),
                "user": "U1",
                "text": "Photos: <https://x.example/p|album>",
            },
            {"ts": str(T0 + 130), "subtype": "channel_join", "user": "U2", "text": "joined"},
        ]
    }
    replies = {
        str(T0 + 60): [
            {"ts": str(T0 + 60), "user": "U1", "text": "parent"},
            {"ts": str(T0 + 90), "user": "U2", "text": "count me in"},
        ]
    }
    adapter = SlackSourceAdapter(
        call=_fake(history, replies, calls), sources=[{"channel": "events", "wing": "homefront"}]
    )
    drawers = _ingest(adapter)
    files = sorted({d.source_file for d in drawers})
    assert len(files) == 2 and all(f.startswith("slack://Homefront/events/") for f in files)
    text = "\n".join(d.content for d in drawers)
    assert "Ada: Potluck at @bo's on Friday" in text
    assert "    [" in text and "bo: count me in" in text
    assert "soup & bread" in text and "album (https://x.example/p)" in text
    assert "joined" not in text
    assert {d.metadata["wing"] for d in drawers} == {"homefront"}
    assert set(calls) <= {
        "auth.test",
        "users.list",
        "conversations.list",
        "conversations.history",
        "conversations.replies",
    }


def test_require_keeps_only_matching_messages_from_a_dm(monkeypatch):
    monkeypatch.setattr("mempalace.sources.slack.configured_markers", lambda: ())
    history = {
        "D9": [
            {"ts": str(T0 + 10), "user": "U1", "text": "☀️ *Today*: 30/90 min _— JP Queue_"},
            {"ts": str(T0 + 20), "user": "U1", "text": "note to self: private thought"},
        ]
    }
    adapter = SlackSourceAdapter(
        call=_fake(history),
        sources=[{"channel": "D9", "wing": "plan", "require": "^☀️.*_— JP Queue_"}],
    )
    text = "\n".join(d.content for d in _ingest(adapter))
    assert "JP Queue" in text and "private thought" not in text


def test_a_marked_chunk_is_labelled_sensitive(monkeypatch):
    monkeypatch.setattr("mempalace.sources.slack.configured_markers", lambda: ("acme-call",))
    history = {
        "C1": [{"ts": str(T0), "user": "U1", "text": "notes from the acme-call this morning"}]
    }
    adapter = SlackSourceAdapter(call=_fake(history), sources=[{"channel": "events"}])
    drawers = _ingest(adapter)
    assert drawers and all(d.metadata["privacy_class"] == "sensitive" for d in drawers)


def test_no_token_is_an_auth_error(monkeypatch):
    monkeypatch.delenv("MEMPALACE_SLACK_TOKEN", raising=False)
    adapter = SlackSourceAdapter(sources=[{"channel": "events"}])
    with pytest.raises(AuthRequiredError):
        _ingest(adapter)


def test_a_sensitive_record_from_an_ordinary_adapter_is_filed_privately(tmp_path, monkeypatch):
    from mempalace.cli import mine_source_adapter
    from mempalace.palace import PRIVATE_COLLECTION_NAME, get_collection
    from mempalace.sources import register, unregister

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("mempalace.sources.slack.configured_markers", lambda: ("acme-call",))
    history = {
        "C1": [
            {"ts": str(T0), "user": "U1", "text": "notes from the acme-call this morning"},
            {"ts": str(T0 + 86400 * 3), "user": "U2", "text": "the garden needs water on Sunday"},
        ]
    }

    class _Fake(SlackSourceAdapter):
        name = "test-slack-fake"

        def __init__(self):
            super().__init__(
                call=_fake(history), sources=[{"channel": "events", "wing": "homefront"}]
            )

    register(_Fake.name, _Fake)
    try:
        palace = str(tmp_path / "palace")
        mine_source_adapter(source_name=_Fake.name, source_path=str(tmp_path), palace_path=palace)
        shared = get_collection(palace).get(include=["documents"])["documents"]
        private = get_collection(palace, collection_name=PRIVATE_COLLECTION_NAME).get(
            include=["documents"]
        )["documents"]
        assert any("garden" in d for d in shared) and not any("acme-call" in d for d in shared)
        assert any("acme-call" in d for d in private) and not any("garden" in d for d in private)
    finally:
        unregister(_Fake.name)
