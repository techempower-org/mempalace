#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Hybrid search: BM25 keyword matching + vector semantic similarity. The
drawer query is the floor — always runs — and closet hits add a rank-based
boost when they agree. Closets are a ranking *signal*, never a gate, so
weak closets (regex extraction on narrative content) can only help, never
hide drawers the direct path would have found.
"""

# The public import path remains ``mempalace.searcher``. Implementation is
# split across this package so PRs can target ranking, sqlite BM25, the
# hybrid pipeline, or CLI output without colliding. Fragments exec into
# this package's globals (the same namespace as the historical module).

import functools
import logging
import math
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Optional

from ..backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    PalaceNotFoundError,
    UnsupportedCapabilityError,
)
from ..backends._inproc_sqlite import open_reader as open_palace_reader
from ..backends.chroma import _filtered_candidate_rows, _fts_candidate_rows
from ..config import MempalaceConfig
from ..date_window import filed_at_in_window, parse_window
from ..embedding import UnknownEmbeddingModelError, _model_error_class, model_error_result
from ..i18n import _canonical_lang, get_stopwords
from ..miner import _open_collection_or_explain
from ..palace import (
    get_closets_collection,
    get_collection,
    resolve_backend_name,
)
from ..provenance import annotate as _annotate_provenance
from ..result_ordering import prefer_curated as _prefer_curated
from ..ratings import net_rating, rating_distance_adjustment
from ..recency import RECENCY_HALFLIFE_DAYS, recency_distance_adjustment

# Closet pointer line format: "topic|entities|→drawer_id_a,drawer_id_b"
# Multiple lines may join with newlines inside one closet document.
_CLOSET_DRAWER_REF_RE = re.compile(r"→([\w,]+)")


logger = logging.getLogger("mempalace_mcp")


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)

_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (ranking before sqlite_bm25, candidates before CLI search).
# Do not reorder this tuple.
_FRAGMENTS = (
    "ranking.py",
    "filters.py",
    "sqlite_bm25.py",
    "candidates.py",
    "query.py",
    "cli_search.py",
    "render.py",
)


def _exec_fragment(filename: str) -> None:
    # Load through SourceFileLoader so the fragment's bytecode is cached in __pycache__ like any module's.
    # exec(compile(read_text())) recompiled every fragment on every process start: ~74 ms for cli alone
    # (fork, 2026-10-22), on the Stop/PreCompact hook path and its 100 ms startup budget.
    from importlib.machinery import SourceFileLoader

    path = _FRAGMENT_DIR / filename
    name = f"{__name__}.{filename[:-3]}"
    exec(SourceFileLoader(name, str(path)).get_code(name), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
