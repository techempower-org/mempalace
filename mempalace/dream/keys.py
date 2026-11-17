"""Keys for a dream run, read quietly from env files instead of systemd ``EnvironmentFile=``.

systemd logs a line of an ``EnvironmentFile=`` it cannot parse, in full, so a secrets file must never be one
(2026-11-14). ``--key-file`` names such a file (0600, ``NAME=value`` lines); the run reads only the names it needs
from it, and only when the environment does not already set them.
"""

from __future__ import annotations

import os
from pathlib import Path

NAMES = ("PALACE_API_KEY", "DREAM_LLM_API_KEY")


def read_var(path: Path, name: str) -> str:
    """One ``NAME=value`` (or ``export NAME=value``) from an env file; "" when absent or unreadable."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""
    for row in text.splitlines():
        row = row.strip()
        if row.startswith("export "):
            row = row[7:].lstrip()
        if row.startswith(name + "="):
            return row.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load(files, names=NAMES, environ=None) -> list:
    """Set each of ``names`` the environment lacks from the first file that has it. Returns the names set (never
    the values)."""
    env = os.environ if environ is None else environ
    loaded = []
    for name in names:
        if env.get(name):
            continue
        for path in files:
            value = read_var(path, name)
            if value:
                env[name] = value
                loaded.append(name)
                break
    return loaded
