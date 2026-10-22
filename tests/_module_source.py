"""Full source text of a mempalace module, whether it is still one file or an exec-fragment package.

Upstream split cli, mcp_server, searcher and palace into packages whose fragment files are exec'd, in the order
listed in ``_FRAGMENTS``, into the package's globals. Source-scanning tests used to read ``mempalace/<name>.py``;
``inspect.getsource(package)`` now returns only ``__init__.py``, so an "X never appears" assertion would pass
vacuously. ``module_source`` returns ``__init__.py`` followed by every fragment in exec order: the same code the
monolith held, in an order that executes identically.
"""

import pathlib
import re

MEMPALACE = pathlib.Path(__file__).resolve().parents[1] / "mempalace"


def module_paths(name: str) -> list[pathlib.Path]:
    mono = MEMPALACE / f"{name}.py"
    if mono.exists():
        return [mono]
    pkg = MEMPALACE / name
    init = pkg / "__init__.py"
    m = re.search(r"_FRAGMENTS\s*=\s*\((.*?)\)", init.read_text(encoding="utf-8"), re.S)
    frags = [s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()] if m else []
    return [init, *(pkg / f for f in frags)]


def module_source(name: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in module_paths(name))
