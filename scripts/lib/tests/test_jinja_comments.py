"""Tests for scripts/lib/jinja_comments.py.

Run: uv run pytest scripts/lib/tests/test_jinja_comments.py
"""

from pathlib import Path

from lib.jinja_comments import strip_jinja_comments
from lib.repo_paths import ANSIBLE

# A template that must stay in the census, so a glob that stops matching fails loudly
# rather than passing over nothing.
SHARED_INGRESSROUTE = ANSIBLE / "templates" / "ingressroute.yml.j2"


def test_a_comment_is_blanked():
    assert strip_jinja_comments("a{# public=false #}b") == "ab"


def test_a_template_with_no_comment_is_returned_byte_for_byte():
    text = "{{ ingressroute(a, b, c, d) }}\n#} not a comment {#\n"
    assert strip_jinja_comments(text) == text


def test_the_lines_a_comment_spanned_survive_as_blank_lines():
    """`glance_facts.compose_images` binds an `image:` to the indented service key above
    it, so a multi-line comment must not join the line before it to the line after."""
    stripped = strip_jinja_comments(
        "  radarr:\n{# a\n   two-line note #}\n    image: x\n"
    )
    assert stripped.splitlines() == ["  radarr:", "", "", "    image: x"]


def test_a_comment_closes_at_its_first_end_marker():
    """Jinja comments do not nest: `#}` ends the one that is open."""
    assert strip_jinja_comments("a{# one #} b {# two #}c") == "a b c"


def test_an_unterminated_comment_is_left_as_written():
    """The deploy refuses such a template, and a reader should see what it refuses."""
    text = "a{# never closed\nb\n"
    assert strip_jinja_comments(text) == text


def test_every_comment_free_template_in_the_tree_is_untouched():
    """The property that makes this safe to add in front of an existing text reader."""
    templates = sorted(Path(ANSIBLE).rglob("*.j2"))
    assert SHARED_INGRESSROUTE in templates
    checked = 0
    for tmpl in templates:
        text = tmpl.read_text()
        if "{#" in text:
            continue
        assert strip_jinja_comments(text) == text, tmpl
        checked += 1
    assert checked >= 100


def test_every_template_in_the_tree_keeps_its_line_count():
    templates = sorted(Path(ANSIBLE).rglob("*.j2"))
    assert SHARED_INGRESSROUTE in templates
    for tmpl in templates:
        text = tmpl.read_text()
        assert strip_jinja_comments(text).count("\n") == text.count("\n"), tmpl
