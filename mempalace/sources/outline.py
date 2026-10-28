"""Outline source adapter: every published document of the configured collections.

Reads an Outline wiki through its API::

    "outline_url": "https://outline.example",
    "outline_sources": [{"collection": "Projects", "wing": "wiki"}]

``outline_sources`` is optional: without it every collection is read into wing
``outline_wiki``. The API token comes from ``MEMPALACE_OUTLINE_TOKEN`` in the
environment, never from the config, and only read methods are called.

Each document is one source, ``outline://<team_id>/<document_id>``: a short
header (collection, title, last update, URL) and the document's markdown,
verbatim, chunked like any text. A document that matches a sensitive marker
(``mempalace.sensitive``) is labelled ``sensitive`` as a whole, and core files it
into the private collection. Each collection read in full is a snapshot scope,
so a document deleted, archived or moved out of it leaves nothing behind.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Callable, Iterator, List, Optional

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

TOKEN_ENV = "MEMPALACE_OUTLINE_TOKEN"
DEFAULT_WING = "outline_wiki"
_PAGE = 100

# fn(method, body) -> parsed JSON; replaced in tests.
OutlineCall = Callable[[str, dict], dict]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token to wherever it points; refuse every one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"refused redirect to {newurl}", headers, fp
        )


def _http_call(base_url: str, token: str) -> OutlineCall:
    if not base_url.startswith("https://"):
        raise SourceNotFoundError("outline: outline_url must be https://")
    opener = urllib.request.build_opener(_NoRedirect)

    def call(method: str, body: dict) -> dict:
        request = urllib.request.Request(
            base_url.rstrip("/") + "/api/" + method,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthRequiredError(f"outline {method}: HTTP {exc.code}") from None
            raise

    return call


def _data(response: dict, method: str):
    """The response's ``data``; anything but a successful envelope raises, so a failed read can
    never pass for an empty collection and publish a scope that would delete it."""
    if not isinstance(response, dict) or response.get("ok") is False or "data" not in response:
        raise RuntimeError(f"outline {method}: unexpected response")
    return response["data"]


def _paged(call: OutlineCall, method: str, body: dict) -> Iterator[dict]:
    offset = 0
    while True:
        page = _data(call(method, dict(body, limit=_PAGE, offset=offset)), method)
        if not isinstance(page, list):
            raise RuntimeError(f"outline {method}: data is not a list")
        yield from page
        if len(page) < _PAGE:
            return
        offset += _PAGE


class OutlineSourceAdapter(BaseSourceAdapter):
    """Ingest an Outline wiki's published documents."""

    name = "outline"
    adapter_version = "1.0.0"
    supported_modes = frozenset({"chunked_content"})
    declared_transformations = frozenset({"outline_document_header", "chunk_exchanges"})
    default_privacy_class = "internal"

    def __init__(
        self,
        call: Optional[OutlineCall] = None,
        sources: Optional[List[dict]] = None,
        base_url: Optional[str] = None,
    ):
        self._call = call
        self._sources = sources
        self._base_url = base_url
        self._scopes: List[dict] = []

    def snapshot_scopes(self) -> List[dict]:
        return list(self._scopes)

    def _config(self) -> dict:
        from ..config import MempalaceConfig

        return MempalaceConfig()._file_config

    def _client(self) -> OutlineCall:
        if self._call is not None:
            return self._call
        base = self._base_url or self._config().get("outline_url")
        if not base:
            raise SourceNotFoundError("outline: set outline_url in the palace config")
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            raise AuthRequiredError(f"outline: set {TOKEN_ENV} (from the vault) to read Outline")
        return _http_call(base, token)

    def _configured(self) -> Optional[List[dict]]:
        sources = (
            self._sources if self._sources is not None else self._config().get("outline_sources")
        )
        if sources is None:
            return None
        if not isinstance(sources, list) or not all(
            isinstance(s, dict) and isinstance(s.get("collection"), str) for s in sources
        ):
            raise SourceNotFoundError(
                "outline: outline_sources must be a list of {collection, wing}"
            )
        return sources

    def ingest(self, *, source: SourceRef, palace: PalaceContext) -> Iterator[DrawerRecord]:
        self._scopes = []
        call = self._client()
        auth = _data(call("auth.info", {}), "auth.info") or {}
        team_id = (auth.get("team") or {}).get("id") or "team"
        base_url = ((auth.get("team") or {}).get("url") or "").rstrip("/")
        collections = list(_paged(call, "collections.list", {}))
        by_name = {c["name"]: c for c in collections}
        sources = self._configured()
        if sources is None:
            wanted = [(c, DEFAULT_WING) for c in collections]
        else:
            missing = [s["collection"] for s in sources if s["collection"] not in by_name]
            if missing:
                raise SourceNotFoundError(f"outline: no collection named {', '.join(missing)}")
            wanted = [(by_name[s["collection"]], s.get("wing") or DEFAULT_WING) for s in sources]
        if len({c["id"] for c, _w in wanted}) != len(wanted):
            raise SourceNotFoundError("outline: outline_sources lists a collection more than once")
        markers = configured_markers()
        scopes = []
        for collection, wing in wanted:
            for doc in _paged(call, "documents.list", {"collectionId": collection["id"]}):
                text = doc.get("text") or ""
                url = doc.get("url") or ""
                if url.startswith("/") and base_url:
                    url = base_url + url
                header = (
                    f"Outline: {collection['name']} / {doc.get('title') or '(untitled)'}\n"
                    f"Updated: {doc.get('updatedAt', '')}"
                    f" by {(doc.get('updatedBy') or {}).get('name', '?')}\n"
                    f"URL: {url}\n"
                )
                body = header + "\n" + text + "\n"
                sensitive = is_marked(body, markers)
                room = detect_convo_room(body)
                chunks = [
                    c if isinstance(c, str) else c.get("content", str(c))
                    for c in chunk_exchanges(body)
                ]
                for i, content in enumerate(chunks):
                    yield DrawerRecord(
                        content=content,
                        source_file=f"outline://{team_id}/{doc['id']}",
                        chunk_index=i,
                        metadata={
                            "wing": wing,
                            "room": room,
                            "outline_team_id": team_id,
                            "outline_collection": collection["name"],
                            "outline_collection_id": collection["id"],
                            "outline_title": doc.get("title") or "",
                            "outline_updated_at": doc.get("updatedAt") or "",
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
                        {"outline_team_id": team_id},
                        {"outline_collection_id": collection["id"]},
                    ]
                }
            )
        # Published only once every collection was read in full.
        self._scopes = scopes

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(
            fields={
                "source_file": FieldSpec(
                    type="string",
                    required=True,
                    description="outline://<team_id>/<document_id>",
                    indexed=True,
                ),
                "outline_collection": FieldSpec(type="string", required=True, description="name"),
                "outline_collection_id": FieldSpec(type="string", required=True, description="id"),
                "outline_title": FieldSpec(type="string", required=True, description="title"),
                "outline_updated_at": FieldSpec(type="string", required=True, description="ISO"),
                "privacy_class": FieldSpec(
                    type="string", required=True, description="internal or sensitive"
                ),
            },
            version=self.adapter_version,
        )

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        sources = self._configured()
        if sources is None:
            return SourceSummary(description="outline: every collection")
        return SourceSummary(
            description="outline: " + ", ".join(s["collection"] for s in sources),
            item_count=len(sources),
        )
