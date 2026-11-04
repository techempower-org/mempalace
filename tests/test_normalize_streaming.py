"""Streaming .jsonl normalize (#412) and the size cap that follows it.

A 601 MB Claude Code transcript on familiar (2026-11-04) was over the 500 MB
whole-file cap, so the convo miner skipped it every time and nothing after
10-31 reached the palace. A .jsonl now streams, so it gets a larger bound.
"""

import json
import os

import pytest

from mempalace import convo_miner, normalize as norm
from mempalace.normalize import (
    _read_transcript_file,
    _try_claude_code_jsonl,
    _try_claude_code_jsonl_lines,
    normalize_conversations,
)


def _session(n=3):
    out = []
    for i in range(n):
        out.append(json.dumps({"type": "user", "message": {"content": f"question {i}"}}))
        out.append(json.dumps({"type": "assistant", "message": {"content": f"answer {i}"}}))
    return "\n".join(out) + "\n"


def test_lines_form_equals_string_form():
    text = "not json\n" + _session(4) + "\n\n"
    assert _try_claude_code_jsonl_lines(text.split("\n")) == _try_claude_code_jsonl(text)
    assert _try_claude_code_jsonl_lines(iter(text.splitlines(True))) == _try_claude_code_jsonl(text)


def test_streamed_file_equals_eager_read(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_session(5), encoding="utf-8")
    eager = _try_claude_code_jsonl(_read_transcript_file(str(p)))
    assert normalize_conversations(str(p)) == [eager]


def test_streamed_file_tolerates_a_bom(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b"\xef\xbb\xbf" + _session(2).encode("utf-8"))
    (conv,) = normalize_conversations(str(p))
    assert "> question 0" in conv


def test_jsonl_over_the_whole_file_cap_still_streams(tmp_path, monkeypatch):
    p = tmp_path / "big.jsonl"
    p.write_text(_session(50), encoding="utf-8")
    monkeypatch.setattr(norm, "TRANSCRIPT_MAX_BYTES", 10)
    (conv,) = normalize_conversations(str(p))
    assert "> question 49" in conv


def test_non_claude_jsonl_over_the_cap_is_refused_by_the_eager_path(tmp_path, monkeypatch):
    p = tmp_path / "other.jsonl"
    p.write_text("\n".join(json.dumps({"x": i}) for i in range(50)) + "\n", encoding="utf-8")
    monkeypatch.setattr(norm, "TRANSCRIPT_MAX_BYTES", 10)
    with pytest.raises(IOError, match="too large"):
        normalize_conversations(str(p))


def test_streaming_bound_applies(tmp_path, monkeypatch):
    p = tmp_path / "big.jsonl"
    p.write_text(_session(50), encoding="utf-8")
    monkeypatch.setattr(norm, "TRANSCRIPT_MAX_BYTES", 10)
    monkeypatch.setattr(norm, "JSONL_STREAM_MAX_BYTES", 10)
    with pytest.raises(IOError, match="too large"):
        normalize_conversations(str(p))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_named_jsonl_fails_instead_of_hanging(tmp_path):
    p = tmp_path / "pipe.jsonl"
    os.mkfifo(p)
    with pytest.raises(IOError):
        normalize_conversations(str(p))


def test_symlinked_jsonl_is_refused(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text(_session(2), encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(IOError, match="symlink"):
        normalize_conversations(str(link))


def test_size_cap_for_exempts_jsonl_only():
    assert convo_miner.size_cap_for("/x/s.jsonl") == norm.JSONL_STREAM_MAX_BYTES
    assert convo_miner.size_cap_for("/x/S.JSONL") == norm.JSONL_STREAM_MAX_BYTES
    assert convo_miner.size_cap_for("/x/s.txt") == convo_miner.MAX_FILE_SIZE
    assert norm.JSONL_STREAM_MAX_BYTES > convo_miner.MAX_FILE_SIZE


def test_scan_keeps_a_big_jsonl_and_drops_a_big_txt(tmp_path, monkeypatch):
    (tmp_path / "big.jsonl").write_text(_session(20), encoding="utf-8")
    (tmp_path / "big.txt").write_text("x" * 2000, encoding="utf-8")
    monkeypatch.setattr(convo_miner, "MAX_FILE_SIZE", 100)
    names = {f.name for f in convo_miner.scan_convos(str(tmp_path))}
    assert "big.jsonl" in names
    assert "big.txt" not in names
