"""Markdown -> the small HTML subset Telegram renders, cut into messages.

Telegram has no Markdown of its own worth the name: headings and list markers
are printed literally, and MarkdownV2 needs half the ASCII table escaped. HTML
is the only mode that survives a model's output, so the summary is translated
once here.
"""

from __future__ import annotations

import re

# Telegram rejects a message over 4096 characters; leave room for the tags an
# unclosed line could add.
LIMIT = 3900

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*$")
_BULLET = re.compile(r"^(\s*)[*+-]\s+")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return _LINK.sub(r'<a href="\2">\1</a>', text)


def to_html(md: str) -> str:
    out = []
    fenced = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            # A fence toggles <pre>; its content is escaped but not formatted.
            out.append("</pre>" if fenced else "<pre>")
            fenced = not fenced
            continue
        if fenced:
            out.append(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            continue
        if _RULE.match(line):
            continue
        heading = _HEADING.match(line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
            continue
        bullet = _BULLET.match(line)
        if bullet:
            line = _BULLET.sub(r"\1• ", line, count=1)
        out.append(_inline(line))
    if fenced:
        out.append("</pre>")
    return "\n".join(out).strip()


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line boundaries so no message ends inside a tag."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single paragraph longer than a message
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts or [""]
