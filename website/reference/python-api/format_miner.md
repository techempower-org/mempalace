# `mempalace.format_miner`

Source: [`mempalace/format_miner.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/format_miner.py)

format_miner.py — proposed for mempalace 3.3.6.

A third miner alongside ``miner.py`` (project files) and ``convo_miner.py``
(chat exports). This one handles **binary office-format documents**:
PDF, DOCX, PPTX, XLSX, RTF, EPUB.

Architecture matches the existing miner-per-content-type pattern:

    mempalace mine &lt;dir>                  → miner.py
    mempalace mine &lt;dir> --mode convos    → convo_miner.py
    mempalace mine &lt;dir> --mode extract   → format_miner.py (this file)

**Read-time conversion**, never modifies source files on disk. The user's
``~/research.pdf`` stays exactly as it was after a mine — the bytes are
read into memory, handed to Microsoft's MarkItDown library for conversion
to Markdown, and the resulting text flows into the normal chunker /
drawer-store pipeline. Conversion artifacts never touch disk.

MarkItDown is an **optional** runtime dependency (declared as an extra).
If the user runs ``mempalace mine --mode extract`` without it installed,
they get a clear ``pip install markitdown`` instruction, not a crash.

**Per-format transformer routing** (verified live on a local mixed-format
test directory, 2026-05-19): MarkItDown 0.1.5 does NOT convert .rtf —
it returns the raw RTF control-code source unchanged. So .rtf is routed
to the purpose-built ``striprtf`` library; all other formats stay on
MarkItDown.

    .pdf, .docx, .pptx, .xlsx, .epub  → MarkItDown
    .rtf                              → striprtf

Both libraries are optional runtime dependencies. If a user tries to
extract a format whose transformer isn't installed, they get a clear
install message (SKIP_NO_MARKITDOWN or SKIP_NO_STRIPRTF), not a crash.

**13 fringe cases handled** (spec finalized 2026-05-19):

    1.  MarkItDown not installed     → SKIP_NO_MARKITDOWN  (clear install msg)
    2.  File too large (> max)        → SKIP_TOO_LARGE
    3.  iCloud cloud-only file        → SKIP_CLOUD_ONLY
    4.  Encrypted PDF                 → SKIP_ENCRYPTED
    5.  Empty file                    → SKIP_EMPTY
    6.  Permission denied             → SKIP_PERMISSION
    7.  Broken symlink                → SKIP_BROKEN_SYMLINK
    8.  Dirty encoding                → recovered via decode_robust
    9.  Windows path semantics        → pathlib throughout
    10. MarkItDown internal crash     → SKIP_EXTRACTION_ERROR
    11. Network / sync timeout        → SKIP_NETWORK_TIMEOUT
    12. Unrecognized extension        → SKIP_UNRECOGNIZED
    13. striprtf not installed        → SKIP_NO_STRIPRTF    (added 2026-05-19
                                                            after live test
                                                            on local RTF files)

Deferred (out of scope): custom PDF parsers for specific document types,
OCR on scanned PDFs, DRM-locked files, pathological corrupt files.
These get reported and skipped.

## Classes

### `class ExtractionStatus(enum.Enum)`

Outcome of an ``extract_text`` call.

Test-friendly enum (each case has its own name) so callers can assert
exactly which path was taken. ``OK`` means text came back; everything
else is a skip variant.

## Functions

### `decode_robust`

```python
def decode_robust(raw: bytes) -> str
```

Decode bytes to text without raising on dirty encodings.

Strategy: UTF-8 first (the clean case). On failure, try CP1252 (handles
legacy smart-quote bytes 0x91-0x9F that surface in older Office docs).
Final fallback is UTF-8 with ``errors='replace'`` so no byte is ever
lost — only made visible as the replacement char.

### `is_icloud_dataless`

```python
def is_icloud_dataless(path: Path) -> bool
```

True if ``path`` is an iCloud cloud-only placeholder (not local).

Two indicators:
  1. The literal ``.icloud`` suffix iCloud uses for offloaded files.
  2. The macOS UF_COMPRESSED / dataless flag on the inode (``st_flags``).

Either signal means MarkItDown would block on file I/O waiting for
iCloud to materialize the bytes, which can hang for minutes. We skip
these and report.

### `extract_text`

```python
def extract_text(path: Union[Path, str], max_file_size: int = DEFAULT_MAX_FILE_SIZE) -> tuple[Optional[str], ExtractionStatus]
```

Convert ``path`` to plain text via MarkItDown, with comprehensive fringe-case handling.

Returns ``(text, ExtractionStatus)`` — text is ``None`` for every skip
case, a non-empty string for ``OK``.

Pure function (no I/O outside the file at ``path``). Source file is
never modified.

### `scan_formats`

```python
def scan_formats(directory: Union[Path, str]) -> list[Path]
```

Walk ``directory`` recursively and return supported files, sorted.

Skips:
  - Hidden / build directories listed in ``palace.SKIP_DIRS``
  - Filenames listed in ``_SKIP_FILENAMES`` (.DS_Store etc.)
  - Symlinks (prevents circular links / processing the same file via
    multiple paths; mirrors ``miner.py`` and ``convo_miner.py``)
  - Files whose suffix isn't in ``SUPPORTED_FORMATS``

Returns a list of ``Path`` objects. The order is deterministic
(sorted by path) so a re-mine processes files in the same order each
time — useful for reproducing bug reports.

### `mine_formats`

```python
def mine_formats(format_dir: str, palace_path: str, wing: Optional[str] = None, agent: str = 'mempalace', limit: int = 0, dry_run: bool = False) -> None
```

Mine a directory of binary office-format files into the palace.

A non-dry-run holds the per-palace write lock for the whole mine, the
same lock ``miner.mine`` and ``convo_miner.mine_convos`` hold. Dry-run
does not write, so it does not take the lock. ``KeyboardInterrupt``
propagates after the summary. A file that fails to file raises
``MineFileErrors`` after the summary.
