# `mempalace.dream`

Source: [`mempalace/dream/__init__.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/__init__.py)

Dreaming: a nightly job that reads what agents and people wrote and proposes changes to it.

It never edits its inputs. Every finding is a *proposal* with its sources and an authority
label; nothing becomes a rule, and no memory changes, until a reviewer merges it. See
``docs/superpowers/specs/2026-11-05-dreaming-decision.md``.

Day 1 is :mod:`mempalace.dream.inventory`: checks that need no model.
