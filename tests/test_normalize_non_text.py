"""Tool output that isn't text is replaced by a size marker before it is filed.

Seen in wing plan_realm_watch (2026-10-20): UTF-16 / U+FFFD fragments from a
binary piped to stdout, and minified-JS lines from a bundled CLI, filed as
drawers and recalled as "essential story".
"""

from mempalace.normalize import _format_tool_result, _omit_non_text

UTF16 = "".join(f"{c}\x00" for c in "list may be incomplete. ") * 4
REPLACEMENT = "�\x0c��^�assertionExp-����x��" * 8
MINIFIED = (
    'function e2s(){return{peerToken:X(C).toString("hex")};if(S===void 0)return{kind:"u"}}' * 20
)
JSON_LINE = ",".join('{"Life Area":"Home","Status":"This Week","n":%d}' % i for i in range(40))
CSS_LINE = "nav.top{display:flex;gap:2px;align-items:center;padding:8px 12px}" * 20
PROSE = "The coordinator hire is off for fall; revisit in January. " * 30


def test_utf16_output_is_omitted():
    assert _omit_non_text(UTF16) == f"[binary output omitted: {len(UTF16)} chars]"


def test_replacement_char_output_is_omitted():
    assert _omit_non_text(REPLACEMENT).startswith("[binary output omitted:")


def test_a_minified_line_is_omitted_and_its_neighbours_kept():
    out = _omit_non_text(f"before\n{MINIFIED}\nafter")
    assert out == f"before\n[minified code line omitted: {len(MINIFIED)} chars]\nafter"


def test_json_css_and_prose_are_kept():
    for text in (JSON_LINE, CSS_LINE, PROSE, "short � text"):
        assert _omit_non_text(text) == text, text[:40]


def test_verbatim_mode_omits_too():
    for tool in ("Bash", "Read", "SomeMcpTool"):
        out = _format_tool_result(UTF16, tool, verbatim=True)
        assert out == f"→ [binary output omitted: {len(UTF16)} chars]", tool


def test_bash_output_with_a_minified_line_keeps_the_rest():
    out = _format_tool_result(f"$ strings cli\n{MINIFIED}\nexit 0", "Bash")
    assert "→ $ strings cli" in out and "→ exit 0" in out
    assert MINIFIED not in out
