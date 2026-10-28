"""Slack source adapter: channel history, one drawer set per channel per day.

Reads the channels listed under ``slack_sources`` in the palace config::

    "slack_sources": [
      {"channel": "events", "wing": "homefront"},
      {"channel": "D0123ABCD", "wing": "plan_realm_watch", "require": "_— JP Queue_"}
    ]

``channel`` is a channel name or id. ``require`` is an optional regular
expression a top-level message must match to be kept: it lets a direct-message
channel yield one kind of message (a daily brief) and nothing else. The token
comes from ``MEMPALACE_SLACK_TOKEN`` in the environment, never from the config,
and only read methods are called.

Each day of a channel becomes one verbatim transcript (``[HH:MM] Name: text``,
thread replies indented under their parent), chunked like a conversation, with
``source_file`` ``slack://<team_id>/<channel_id>/<YYYY-MM-DD>`` (stable ids, and the day in
``slack_timezone`` from the palace config, UTC by default). A chunk that matches a
sensitive marker (``mempalace.sensitive``) labels its whole day ``sensitive``,
and core files that day into the private collection. Every run re-reads the full history and
declares each channel a snapshot scope, so core removes whatever the run no longer
produced: a shrunk or vanished day, a message the filter now excludes, or a day's
copy in the collection it no longer belongs to.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional

from ..convo_miner import chunk_exchanges, detect_convo_room
from ..sensitive import configured_markers, is_marked
from .base import (
    AdapterSchema,
    AuthRequiredError,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    RouteHint,
    SourceNotFoundError,
    SourceRef,
    SourceSummary,
)
from .context import PalaceContext

logger = logging.getLogger(__name__)

TOKEN_ENV = "MEMPALACE_SLACK_TOKEN"
_API = "https://slack.com/api/"
_MENTION = re.compile(r"<@([A-Z0-9]+)>")
_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")

# fn(method, params) -> parsed JSON; replaced in tests.
SlackCall = Callable[[str, Dict[str, str]], dict]


def _http_call(token: str) -> SlackCall:
    opener = urllib.request.build_opener()

    def call(method: str, params: Dict[str, str]) -> dict:
        url = _API + method + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        for attempt in range(5):
            try:
                with opener.open(request, timeout=60) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                time.sleep(int(exc.headers.get("Retry-After", "5")))
        raise RuntimeError("unreachable")

    return call


def _ok(response: dict, method: str) -> dict:
    if not response.get("ok"):
        error = response.get("error", "unknown")
        if error in ("not_authed", "invalid_auth", "token_revoked", "missing_scope"):
            raise AuthRequiredError(f"slack {method}: {error}")
        raise RuntimeError(f"slack {method}: {error}")
    return response


def _paged(call: SlackCall, method: str, key: str, params: Dict[str, str]) -> Iterator[dict]:
    cursor = ""
    while True:
        page = dict(params, limit="200", **({"cursor": cursor} if cursor else {}))
        response = _ok(call(method, page), method)
        yield from response.get(key, [])
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return


def _render(text: str, names: Dict[str, str]) -> str:
    text = _MENTION.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text or "")
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _local(ts: str, tz) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=tz)


def _line(message: dict, names: Dict[str, str], tz, indent: str = "") -> str:
    when = _local(message["ts"], tz).strftime("%H:%M")
    who = names.get(message.get("user", ""), message.get("username") or message.get("user") or "?")
    body = _render(message.get("text", ""), names).strip()
    return indent + f"[{when}] {who}: " + body.replace("\n", "\n" + indent + "    ")


class SlackSourceAdapter(BaseSourceAdapter):
    """Ingest the Slack channels configured under ``slack_sources``."""

    name = "slack"
    adapter_version = "1.0.0"
    supported_modes = frozenset({"chunked_content"})
    declared_transformations = frozenset({"slack_day_transcript", "chunk_exchanges"})
    default_privacy_class = "pii_potential"

    def __init__(
        self,
        call: Optional[SlackCall] = None,
        sources: Optional[List[dict]] = None,
        tz_name: Optional[str] = None,
    ):
        self._call = call
        self._sources = sources
        self._tz_name = tz_name
        self._scopes: List[dict] = []

    def snapshot_scopes(self) -> List[dict]:
        return list(self._scopes)

    def _tz(self):
        name = self._tz_name
        if name is None:
            from ..config import MempalaceConfig

            name = MempalaceConfig()._file_config.get("slack_timezone", "UTC")
        if name == "UTC":
            return timezone.utc
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)

    def _client(self) -> SlackCall:
        if self._call is not None:
            return self._call
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            raise AuthRequiredError(f"slack: set {TOKEN_ENV} (from the vault) to read Slack")
        return _http_call(token)

    def _configured(self) -> List[dict]:
        if self._sources is not None:
            return self._sources
        from ..config import MempalaceConfig

        sources = MempalaceConfig()._file_config.get("slack_sources", [])
        if not isinstance(sources, list) or not all(
            isinstance(s, dict) and isinstance(s.get("channel"), str) for s in sources
        ):
            raise SourceNotFoundError(
                "slack: slack_sources must be a list of {channel, wing, require?}"
            )
        return sources

    def ingest(self, *, source: SourceRef, palace: PalaceContext) -> Iterator[DrawerRecord]:
        self._scopes = []
        sources = self._configured()
        if not sources:
            raise SourceNotFoundError("slack: no slack_sources configured")
        call = self._client()
        tz = self._tz()
        auth = _ok(call("auth.test", {}), "auth.test")
        team, team_id = auth.get("team", "slack"), auth.get("team_id", "T")
        scopes = []
        names = {
            u["id"]: (u.get("real_name") or u.get("name") or u["id"])
            for u in _paged(call, "users.list", "members", {})
        }
        channels = {
            c["name"]: c["id"]
            for c in _paged(
                call,
                "conversations.list",
                "channels",
                {"types": "public_channel,private_channel", "exclude_archived": "true"},
            )
        }
        markers = configured_markers()
        for spec in sources:
            channel = spec["channel"]
            channel_id = channels.get(channel, channel)
            label = channel if channel in channels else channel_id
            wing = spec.get("wing") or "slack"
            require = re.compile(spec["require"]) if spec.get("require") else None
            days: Dict[str, List[str]] = defaultdict(list)
            messages = sorted(
                _paged(call, "conversations.history", "messages", {"channel": channel_id}),
                key=lambda m: float(m["ts"]),
            )
            seen = set()
            for message in messages:
                if message.get("subtype") in ("channel_join", "channel_leave"):
                    continue
                # A reply also sent to the channel (thread_broadcast) is filed under its parent.
                if message.get("thread_ts") and message.get("thread_ts") != message.get("ts"):
                    continue
                if message["ts"] in seen:
                    continue
                if require and not require.search(message.get("text") or ""):
                    continue
                seen.add(message["ts"])
                day = _local(message["ts"], tz).strftime("%Y-%m-%d")
                days[day].append(_line(message, names, tz))
                if message.get("reply_count") and not require:
                    replies = _paged(
                        call,
                        "conversations.replies",
                        "messages",
                        {"channel": channel_id, "ts": message["ts"]},
                    )
                    for reply in replies:
                        if reply.get("ts") != message["ts"] and reply["ts"] not in seen:
                            seen.add(reply["ts"])
                            days[day].append(_line(reply, names, tz, indent="    "))
            for day, lines in sorted(days.items()):
                transcript = f"Slack #{label} ({team}), {day}\n" + "\n".join(lines) + "\n"
                room = detect_convo_room(transcript)
                chunks = [
                    c if isinstance(c, str) else c.get("content", str(c))
                    for c in chunk_exchanges(transcript)
                ]
                # Checked on the whole day, so a marker split by chunking still counts, and one hit
                # files the whole day privately (fail closed).
                sensitive = is_marked(transcript, markers)
                for i, content in enumerate(chunks):
                    yield DrawerRecord(
                        content=content,
                        source_file=f"slack://{team_id}/{channel_id}/{day}",
                        chunk_index=i,
                        metadata={
                            "wing": wing,
                            "room": room,
                            "slack_channel": label,
                            "slack_channel_id": channel_id,
                            "slack_team_id": team_id,
                            "slack_date": day,
                            "privacy_class": "sensitive"
                            if sensitive
                            else self.default_privacy_class,
                        },
                        route_hint=RouteHint(wing=wing, room=room),
                    )
            scopes.append(
                {
                    "$and": [
                        {"adapter_name": self.name},
                        {"slack_team_id": team_id},
                        {"slack_channel_id": channel_id},
                    ]
                }
            )
        # Published only once every channel was read in full, so core never deletes on a partial run.
        self._scopes = scopes

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(
            fields={
                "source_file": FieldSpec(
                    type="string",
                    required=True,
                    description="slack://<team>/<channel>/<YYYY-MM-DD>",
                    indexed=True,
                ),
                "slack_channel": FieldSpec(type="string", required=True, description="channel"),
                "slack_date": FieldSpec(type="string", required=True, description="local day"),
                "privacy_class": FieldSpec(
                    type="string", required=True, description="pii_potential or sensitive"
                ),
            },
            version=self.adapter_version,
        )

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        try:
            sources = self._configured()
        except SourceNotFoundError as exc:
            return SourceSummary(description=str(exc))
        return SourceSummary(
            description="slack: " + ", ".join(s["channel"] for s in sources),
            item_count=len(sources),
        )
