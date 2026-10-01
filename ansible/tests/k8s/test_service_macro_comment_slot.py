#!/usr/bin/env python3
"""`service()`'s caller slot puts a comment on the line it governs, and emits nothing otherwise.

WHY THIS EXISTS. `ansible/templates/service.yml.j2` grew a `{% call %}` slot so tdarr could
adopt the macro without hoisting its "The web UI only" comment above `apiVersion:`. The whole
value of that comment is its POSITION — it explains why the ports list holds one entry, so it
has to sit directly above `- port:`. Nothing else checks that: the manifest validator parses
YAML, and a parser drops comments, so a slot that emitted the comment in the wrong place, or
`service()` reverting to no slot at all, both read green there.

The second test is the other half. A slot that emits a stray blank line for the plain form
would change the rendered bytes of the 25 roles `ansible/templates/service-default.yaml.j2`
renders a Service for, and a blank line inside a YAML block sequence is invisible in
review. bazarr is asked as one of those 25 — it ships no Service template of its own, so the
plain call under test is the shared default's.

Run: uv run pytest ansible/tests/k8s/test_service_macro_comment_slot.py
"""

from _k8s_render import render_role_template


def test_tdarr_comment_sits_on_the_line_above_its_port():
    """The slot's body renders between `ports:` and the entry it explains."""
    lines = render_role_template("tdarr", "service.yaml.j2").splitlines()
    ports = lines.index("  ports:")
    assert lines[ports + 1].strip().startswith("# The web UI only"), (
        "tdarr's ports comment no longer sits directly under `ports:` — rendered:\n"
        + "\n".join(lines)
    )
    assert lines[ports + 2].lstrip().startswith("- port:"), (
        "the comment no longer sits directly above the port entry it explains — rendered:\n"
        + "\n".join(lines)
    )


def test_a_plain_caller_gets_no_slot_line():
    """The shared default calls `service()` without a body, so `ports:` is followed by the entry."""
    lines = render_role_template("bazarr", "service-default.yaml.j2").splitlines()
    ports = lines.index("  ports:")
    assert lines[ports + 1].lstrip().startswith("- port:"), (
        "the caller slot emitted a line for a plain call — rendered:\n"
        + "\n".join(lines)
    )
