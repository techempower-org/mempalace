# `mempalace.normalize`

Source: [`mempalace/normalize.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/normalize.py)

normalize.py — Convert any chat export format to MemPalace transcript format.

Supported:
    - Plain text with > markers (pass through)
    - Claude.ai JSON export
    - ChatGPT conversations.json (a single conversation, or the top-level
      array of them that a real data export ships)
    - Claude Code JSONL (with tool_use/tool_result block capture)
    - OpenAI Codex CLI JSONL
    - Gemini CLI JSONL (~/.gemini/tmp/&lt;project_hash>/chats/session-*.jsonl)
    - Pi agent JSONL
    - Gemini CLI / Google AI Studio JSON sessions (contents / messages / flat list)
    - Continue.dev session JSON (~/.continue/sessions/*.json)
    - Slack JSON export
    - Plain text (pass through for paragraph chunking)

No API key. No internet. Everything local.

## Classes

### `class UnparsedCodexTranscriptError(ValueError)`

A recognized Codex rollout did not yield a supported conversation.

## Functions

### `strip_noise`

```python
def strip_noise(text: str, verbatim: bool = False) -> str
```

Remove system tags, hook output, and Claude Code UI chrome from text.

All patterns are line-anchored. User prose that happens to mention these
strings inline (e.g., documenting them) is preserved verbatim.

When ``verbatim=True``, returns ``text`` unchanged (system tags / hook
chrome / collapsed-output markers are real transcript content for
callers who want a full record).

### `normalize`

```python
def normalize(filepath: str, verbatim: bool = False) -> str
```

Load a file and normalize to transcript format if it's a chat export.
Plain text files pass through unchanged.

When ``verbatim=True``, the Claude Code JSONL parser preserves system
tags, hook output, and full tool input/output (no head/tail collapse,
no byte caps, no Read/Edit/Write omission). Other transcript formats
are already verbatim — the flag is a no-op for them.

### `normalize_conversations`

```python
def normalize_conversations(filepath: str, verbatim: bool = False) -> list
```

Like normalize(), but keeps each conversation in a bundle export as a
separate string instead of joining them into one.

A Claude.ai privacy export packs every conversation into a single JSON
file, and normalize() joins them with "\n\n".join(...) into one blob.
That collapses conversation boundaries, so content-hash dedup keyed on
the whole file breaks the moment the bundle is re-exported with one new
conversation added — the file-level hash changes even though none of
the existing conversations did. This returns the pieces un-joined so
callers can hash and dedup per conversation instead.

A ChatGPT data export is a bundle for the same reason: its
``conversations.json`` is an array of conversations, so it splits per
conversation too.

Non-bundle formats (a single Claude Code session, plain text, ...)
always normalize to one conversation, so this returns a one-element
list for those — identical dedup granularity to before.
