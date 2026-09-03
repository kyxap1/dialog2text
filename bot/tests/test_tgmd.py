from src.tgmd import chunks, to_html


def test_headings_bullets_and_inline_markup_become_telegram_html():
    md = (
        "# Title\n"
        "## Section\n"
        "* **bold** item\n"
        "  - nested `code`\n"
        "---\n"
        "see [docs](https://x.dev) & <tags>\n"
    )
    assert to_html(md) == (
        "<b>Title</b>\n"
        "<b>Section</b>\n"
        "• <b>bold</b> item\n"
        "  • nested <code>code</code>\n"
        'see <a href="https://x.dev">docs</a> &amp; &lt;tags&gt;'
    )


def test_fenced_block_is_escaped_but_not_formatted():
    assert to_html("```\na * b < c\n```") == "<pre>\na * b &lt; c\n</pre>"


def test_chunks_split_on_line_boundaries_and_respect_the_limit():
    text = "\n".join(f"line {i}" for i in range(100))
    parts = chunks(text, limit=50)
    assert all(len(p) <= 50 for p in parts)
    assert "\n".join(parts) == text  # nothing lost, nothing duplicated


def test_a_single_line_longer_than_the_limit_is_cut():
    assert chunks("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]
