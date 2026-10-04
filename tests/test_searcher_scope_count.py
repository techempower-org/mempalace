"""The in-scope count must not read every drawer in the scope.

``_count_in_scope`` reports ``available_in_scope`` on every scoped search. It
used to count a filtered scope by paging ``get(include=[])`` 5,000 ids at a
time, because ChromaDB's ``count()`` takes no ``where``. On the postgres
backend that read the whole scope, and ``include=[]`` still decoded every row's
metadata. Profiled on the production palace on 2026-10-04, one
``wing="memorypalace"`` search spent 6.93 s of 7.0 s there: 12 ``get`` pages,
57,063 rows. The rest of the search took about 0.1 s. That was the whole
"palace slow (>4s)" timeout on the auto-query hook.

A backend that can count a filtered scope directly now says so through the
optional ``count_where``. Backends that can't, return ``None`` and keep the
paged count.
"""

from __future__ import annotations

from typing import Optional

import pytest

from mempalace.backends.base import BaseCollection
from mempalace.backends.embedding_wrapper import EmbeddingCollection
from mempalace.searcher import _count_in_scope

WING = {"wing": "memorypalace"}


class _Paged:
    """A collection with no filtered count: the paged path must still work."""

    def __init__(self, n: int):
        self.n = n
        self.get_calls = 0

    def count(self) -> int:
        return self.n

    def get(self, *, limit, offset, include, where):
        self.get_calls += 1
        ids = [f"d{i}" for i in range(offset, min(offset + limit, self.n))]
        return {"ids": ids}


class _Counted(_Paged):
    """A collection that can count a filtered scope in one call."""

    def __init__(self, n: int, answer: Optional[int] = "same"):
        super().__init__(n)
        self.answer = n if answer == "same" else answer
        self.count_where_calls = []

    def count_where(self, where=None):
        self.count_where_calls.append(where)
        return self.answer


class _Raising(_Paged):
    def count_where(self, where=None):
        raise RuntimeError("planner error")


def test_filtered_scope_uses_count_where_and_never_pages():
    col = _Counted(57_063)
    assert _count_in_scope(col, WING) == 57_063
    assert col.count_where_calls == [WING]
    assert col.get_calls == 0, "a backend that can count must not be paged through"


def test_none_from_count_where_falls_back_to_paging():
    col = _Counted(12_345, answer=None)
    assert _count_in_scope(col, WING) == 12_345
    assert col.get_calls == 3  # 5000 + 5000 + 2345


def test_a_collection_without_count_where_still_pages():
    col = _Paged(7_001)
    assert _count_in_scope(col, WING) == 7_001
    assert col.get_calls == 2


def test_a_raising_count_where_falls_back_to_paging():
    col = _Raising(42)
    assert _count_in_scope(col, WING) == 42


def test_unfiltered_scope_still_uses_count():
    col = _Counted(99)
    assert _count_in_scope(col, {}) == 99
    assert col.count_where_calls == [] and col.get_calls == 0


def test_base_collection_default_is_not_supported():
    """The ABC default must say "can't", never guess a number."""

    class _Bare(BaseCollection):
        def add(self, **kw): ...
        def upsert(self, **kw): ...
        def update(self, **kw): ...
        def query(self, **kw): ...
        def get(self, **kw): ...
        def delete(self, **kw): ...
        def count(self) -> int:
            return 0

    assert _Bare().count_where({"wing": "x"}) is None


def test_embedding_wrapper_forwards_count_where_to_the_backend():
    """``BaseCollection`` defines ``count_where``, so MRO resolves it on the
    wrapper before ``__getattr__`` fires. Without an explicit forwarder, every
    wrapped postgres collection would answer the base ``None`` and silently
    go back to paging (the facet_counts / get_all_metadata shadow pattern)."""
    inner = _Counted(314)
    wrapped = EmbeddingCollection(inner)
    assert wrapped.count_where(WING) == 314
    assert inner.count_where_calls == [WING]


@pytest.mark.parametrize("bad", [-1, "12", 3.5, True])
def test_a_non_count_answer_is_not_trusted(bad):
    """Only a non-negative int is a count; anything else means "page instead"."""
    col = _Counted(9, answer=bad)
    assert _count_in_scope(col, WING) == 9
    assert col.get_calls == 1
