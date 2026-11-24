"""``mempalace-backfill-age`` takes its DSN from the env when --dsn is absent (#583).

A DSN on the command line carries the password into ``ps`` output on the
host; the env fallback keeps it out of the process list.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mempalace import backfill_age


@pytest.fixture
def fake_backfill():
    with patch.object(backfill_age, "backfill", return_value={"ok": True}) as m:
        yield m


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MEMPALACE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("MEMPALACE_PG_DSN", raising=False)


def test_dsn_from_flag(fake_backfill):
    assert backfill_age.main(["--dsn", "postgresql://flag/db"]) == 0
    assert fake_backfill.call_args.kwargs["dsn"] == "postgresql://flag/db"


def test_flag_wins_over_env(fake_backfill, monkeypatch):
    monkeypatch.setenv("MEMPALACE_POSTGRES_DSN", "postgresql://env/db")
    backfill_age.main(["--dsn", "postgresql://flag/db"])
    assert fake_backfill.call_args.kwargs["dsn"] == "postgresql://flag/db"


def test_dsn_from_postgres_dsn_env(fake_backfill, monkeypatch):
    monkeypatch.setenv("MEMPALACE_POSTGRES_DSN", "postgresql://env/db")
    assert backfill_age.main([]) == 0
    assert fake_backfill.call_args.kwargs["dsn"] == "postgresql://env/db"


def test_dsn_from_pg_dsn_env(fake_backfill, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PG_DSN", "postgresql://alias/db")
    backfill_age.main([])
    assert fake_backfill.call_args.kwargs["dsn"] == "postgresql://alias/db"


def test_no_dsn_anywhere_exits_with_usage_error(fake_backfill):
    with pytest.raises(SystemExit) as exc:
        backfill_age.main([])
    assert exc.value.code == 2
    fake_backfill.assert_not_called()
