"""The hook and `reconcile-docs` must agree on what a curated doc is (#529).

The hook (`~/.claude/hooks/palace-memory-sync.sh`) decides by matching a path;
`reconcile-docs` decides by enumerating the filesystem. If they disagree, a doc
is either indexed twice under different wings or missed by both — and nobody
finds out, because neither side ever sees the other's answer.

⭐ There is no single hook pattern to agree WITH. The hook has two matchers:

    Write/Edit path   case "$FILE" in "$HOME"/Projects/*/CLAUDE.md|…/docs/*.md)
    Bash path         grep -qE '/Projects/[^ /]+/(CLAUDE\\.md|docs/[^ ]*\\.md)'

and they disagree with each other about worktrees, because in a shell `case`
`*` spans `/` while `[^ /]+` does not. Measured 2026-09-18:

    …/.claude/worktrees/wt/docs/specs/a.md    case: MATCH    regex: no

The Bash regex is canonical here — the palace host does not carry
`.claude/worktrees` at all, so a worktree path that matches is worse than one
that does not: it fires, travels, and is dropped on arrival with a SKIP.

These tests pin the agreement, and pin the disagreement as a KNOWN hook bug
rather than hiding it. dotfiles `d0fa9d6` since added early exits for worktree
paths, glob tokens and non-existent files, which stops the over-match reaching
the palace host; the two matchers are still separately written, so this test
is what notices if they drift again.
"""

import os
import re
import subprocess

import pytest

from mempalace import cli


# Vendored from ~/.claude/hooks/palace-memory-sync.sh. Kept here as literals so
# the suite runs on a machine that has no hook installed (CI); the live file is
# cross-checked below when it is present.
HOOK_BASH_REGEX = r"/Projects/[^ /]+/(CLAUDE\.md|docs/[^ ]*\.md)"
HOOK_CASE_GLOBS = ("$HOME/Projects/*/CLAUDE.md", "$HOME/Projects/*/docs/*.md")

HOME = "/home/jp"
FIXTURES = [
    # (path, is a curated doc the palace host can serve?)
    (f"{HOME}/Projects/memorypalace/CLAUDE.md", True),
    (f"{HOME}/Projects/memorypalace/docs/specs/a.md", True),
    (f"{HOME}/Projects/2g/docs/deep/nested/b.md", True),
    (f"{HOME}/Projects/memorypalace/.claude/worktrees/wt/CLAUDE.md", False),
    (f"{HOME}/Projects/memorypalace/.claude/worktrees/wt/docs/specs/a.md", False),
    (f"{HOME}/Projects/memorypalace/scratch/notes.md", False),
    (f"{HOME}/Projects/memorypalace/README.md", False),
]


def _bash_matches(path):
    return re.search(HOOK_BASH_REGEX, path) is not None


def _case_matches(path):
    """Shell `case` semantics, where `*` spans `/` — unlike fnmatch on POSIX."""
    script = 'case "$1" in %s) echo MATCH;; *) echo no;; esac' % "|".join(
        g.replace("$HOME", HOME) for g in HOOK_CASE_GLOBS
    )
    out = subprocess.run(
        ["bash", "-c", script, "_", path], capture_output=True, text=True
    ).stdout.strip()
    return out == "MATCH"


class TestTheCanonicalPatternIsTheBashRegex:
    @pytest.mark.parametrize("path,is_curated", FIXTURES)
    def test_the_regex_agrees_with_what_the_palace_host_can_serve(self, path, is_curated):
        assert _bash_matches(path) is is_curated, (
            "the canonical matcher disagrees about %s; the palace host carries "
            "only the main checkout, so a worktree path must NOT match" % path
        )

    @pytest.mark.parametrize("path,is_curated", FIXTURES)
    def test_the_enumerator_agrees_with_the_canonical_matcher(self, path, is_curated, tmp_path):
        """Build the path for real and enumerate it, rather than re-implementing
        the rule in the assertion — a test that restates the implementation
        passes when both are wrong together."""
        rel = path[len(f"{HOME}/Projects/") :]
        project = rel.split("/", 1)[0]
        root = tmp_path / project
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# doc\n")

        found, _total = cli._curated_doc_paths(str(root), limit=None)
        enumerated = str(target) in found

        # The enumerator walks a project root, so a worktree file is reachable
        # only via `.claude/worktrees/...` inside that root — which it must not
        # descend into for the same reason the regex excludes it.
        assert enumerated is is_curated, (
            "enumeration disagrees with the canonical matcher for %s" % rel
        )


class TestTheHooksTwoMatchersDisagreeAboutWorktrees:
    """Documented, not hidden: this is a live defect in the hook.

    dotfiles d0fa9d6 guards the consequence (an early exit on
    `*/.claude/worktrees/*`), so nothing reaches the palace host — but the two
    matchers are still written separately, so the disagreement is still there
    to be re-introduced by anyone who edits one and not the other.
    """

    WORKTREE_DOC = f"{HOME}/Projects/memorypalace/.claude/worktrees/wt/docs/specs/a.md"

    def test_the_case_glob_over_matches_a_worktree_path(self):
        assert _case_matches(self.WORKTREE_DOC) is True, (
            "if this now fails the hook's glob was tightened — good; retire this "
            "test and the guard that compensates for it"
        )

    def test_the_bash_regex_does_not(self):
        assert _bash_matches(self.WORKTREE_DOC) is False

    def test_and_therefore_they_disagree(self):
        assert _case_matches(self.WORKTREE_DOC) != _bash_matches(self.WORKTREE_DOC), (
            "the two matchers now agree — update this file and drop the "
            "worktree early-exit guard in the hook if it is no longer needed"
        )


class TestTheLiveHookStillCarriesTheseMatchers:
    """Cross-check the vendored literals against the installed hook.

    Skipped where the hook is not installed, so CI stays green — but on a
    machine that HAS it, a drift between this file and the real one is caught
    rather than assumed away.
    """

    # NOT os.path.expanduser: conftest repoints HOME at a temp dir, so `~`
    # resolves there and this whole class skipped on the one machine that has
    # the hook installed — a skip that reads exactly like coverage. The passwd
    # entry ignores $HOME and answers about the real account.
    import pwd as _pwd

    HOOK = os.path.join(
        _pwd.getpwuid(os.getuid()).pw_dir, ".claude", "hooks", "palace-memory-sync.sh"
    )

    @pytest.mark.skipif(not os.path.isfile(HOOK), reason="hook not installed here")
    def test_the_vendored_regex_is_the_installed_one(self):
        text = open(self.HOOK, encoding="utf-8").read()
        assert HOOK_BASH_REGEX in text, (
            "the hook's Bash matcher changed; update HOOK_BASH_REGEX and re-check "
            "that the enumerator still agrees with it"
        )

    @pytest.mark.skipif(not os.path.isfile(HOOK), reason="hook not installed here")
    def test_the_worktree_guard_from_d0fa9d6_is_present(self):
        text = open(self.HOOK, encoding="utf-8").read()
        assert "*/.claude/worktrees/*) exit 0" in text, (
            "the worktree early-exit is gone; the case-glob over-match would "
            "again send worktree paths to the palace host, where they SKIP"
        )
