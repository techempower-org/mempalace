# `mempalace.exhaust`

Source: [`mempalace/exhaust.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/exhaust.py)

One definition of "this drawer is the palace's own exhaust, not knowledge".

Every retrieval path needs the same answer, and until now three of them
answered separately: ``auto_query.runner`` had a room set and a prefix tuple,
``layers.Layer1`` had its own copy of both, and nothing anywhere knew about a
diff hunk. A predicate that is duplicated is a predicate that drifts, and the
drift showed up exactly where it hurt most — see below.

WHAT COUNTS AS EXHAUST, and why each shape is here (all measured on the live
palace, wings ``memorypalace`` and ``2g``, 2026-09-11):

* **Bookkeeping rooms and diary ids** — ``AUTO-SAVE:…`` checkpoints, session
  manifests, compaction summaries. The original finding (#421/#423): L1 led
  with them and agents read the palace as their own exhaust.

* **Harness prompt-echo** — the instruction text the harness mirrors back into
  the transcript. ``> you can check for comments``,
  ``> &lt;command-message>exit&lt;/command-message>``,
  ``> &lt;teammate-message teammate_id="team-lead" …``. It is *our* prose, filed
  as if it were the user's memory.

* **Diff scaffolding and diff bodies** — ``Changed files (you may Read these
  and any other file in the repo):``, ``Unified diff (only + lines are new):``,
  ``=== DIFF: tools/bringup-full.sh ===``, ``@@ -89,17 +89,27 @@ …``, and the
  ``+``-prefixed hunk bodies that follow. A patch fragment is never the
  essential story of a wing; the review *conclusion* is, and that survives.

* **Tool-call payloads** — ``&#123;"success":true,"message":"Message sent to
  team-lead's inbox","msg_id":…,"routing":&#123;…}}``. The receipt of an action, not
  the action's content.

WHY THIS MATTERS MORE SINCE #458: post-compaction recovery injects wake-up L1
as the *only* context an agent gets back. Before #458 a noisy L1 was an
annoyance you could scroll past; now it is the whole inheritance. Measured
before this change, ``memorypalace``'s L1 spent 9 of its 16 story lines on one
code review's diff — prompt, scaffolding, four hunk fragments and a truncated
byte-string — and ``2g``'s spent two on a SendMessage receipt and a quoted
teammate block.

THE BIAS IS DELIBERATE AND ONE-DIRECTIONAL: these predicates must never eat a
real finding. Every rule below is anchored on a shape the harness emits and a
human would not write — a diff header, a JSON receipt, a quoted harness tag —
rather than on topic, length or "looks technical". Where a rule could plausibly
catch human prose (a markdown blockquote, a fenced AT-command dump), it is
narrowed until it cannot.

⚠️ HOW TO CHECK A CHANGE TO THIS FILE, learned the hard way. The first audit ran
over 1,134 drawers — 0.67% of the two wings — and reported "zero findings lost".
A full-population pass over 168,390 drawers then found 19 that were lost, and
the sample had contained ZERO instances of the shape responsible, so no care in
reading its drop list could have surfaced them. A sample tells you a rule is
wrong; only the population tells you it is right. Audit the whole wing, and read
the drop list rather than the pass rate.

## Functions

### `is_exhaust_text`

```python
def is_exhaust_text(text)
```

True when the drawer's CONTENT is bookkeeping, echo or a patch fragment.

### `is_exhaust`

```python
def is_exhaust(text, room = '', drawer_id = '')
```

Full predicate: bookkeeping room, diary id, or an exhaust-shaped body.
