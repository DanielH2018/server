"""Render every Jinja-templated shell script, for tests that need to assert on the OUTPUT.

`*.sh.j2` is the third template class `_k8s_render._render_all` skips, after the Pi's Compose
and config templates (`_compose_render.rendered_texts`) and every k8s role's `Dockerfile*.j2`
(`_k8s_render.rendered_build_texts`). A guard that reads a shell template's SOURCE matches
`{{ ... }}` the moment a value it asserts on — a retry count, a URL, a threshold — moves into
a role default, and a pattern that matches nothing passes (#3178).

The render goes through `validate/shell_templates.py`, the gate that already renders these for
its `bash -n` and shellcheck pass, so what a guard asserts on cannot drift from what that gate
lints. Secret values render as `STUB` (the `DECIDED:` rule in `lib/render_guard.py`), so a
guard built on this keys on field names, never on credential-shaped values.

A template is addressed by its (plane, role, name) triple rather than a role name: shell
templates live on all three planes and `k8s/crowdsec` ships four of them, so a bare role name
is ambiguous. `ansible/tests/repo/test_secret_rendering_host_scripts_have_no_log.py` already
names them that way.
"""

import re
from pathlib import Path

from lib.k8s_context import resolve_vars
from lib.repo_paths import ROLES
from validate.shell_templates import (
    base_context,
    discover_templates,
    template_context,
)
from validate.validate_lib.shell_lint import render_template


def shell_template_path(plane: str, role: str, template: str) -> Path:
    """The template file for one (plane, role, name) triple, asserting it exists.

    A renamed or moved template fails here naming what is missing, rather than handing a
    caller a render of nothing.
    """
    path = ROLES / plane / role / "templates" / template
    assert path.is_file(), f"no such shell template: {path}"
    return path


def render_shell_script(
    plane: str, role: str, template: str, overrides: dict | None = None
) -> str:
    """One shell template rendered as text, with `overrides` laid over the render context.

    Uncached, for a guard that needs a value the inventory does not hold — the artifacts sync
    guard runs the script for real and points `artifacts_peer_dir` at a `tmp_path`. A guard
    that only reads the script wants `rendered_shell_text`, which renders the tree once.

    Fails the calling test on a render error rather than returning nothing, so a template that
    stopped rendering cannot read as a script with no findings.
    """
    path = shell_template_path(plane, role, template)
    ctx = template_context(path, overrides=overrides)
    try:
        return render_template(path, ctx)
    except RuntimeError as exc:
        raise AssertionError(f"{plane}/{role}/{template}: {exc}") from exc


def render_shell_texts(
    overrides: dict, templates: list[Path] | None = None
) -> tuple[tuple[str, str, str, str], ...]:
    """`rendered_shell_texts()` with `overrides` laid over every render, uncached.

    For a census at a value the inventory does not hold: set a secret to a sentinel, and every
    script whose render reaches it carries the sentinel, aliases included (#3191).

    The overrides go into the base BEFORE anything resolves, and on top again after.
    `template_context` resolves a role's defaults against the base alone, so a default aliasing
    an overridden secret (`x: "{{ some_secret }}"`) would otherwise resolve to the secret's
    STUB — the trap `_k8s_render._render_texts` names too. The base is resolved here as well,
    so an all.yml alias arrives expanded rather than as literal braces.

    `templates` defaults to every `*.sh.j2` the gate discovers; a test hands it paths under a
    `tmp_path` tree laid out `<plane>/<role>/templates/<name>.sh.j2`.
    """
    raw = {**base_context(), **overrides}
    base = resolve_vars(raw, raw)
    texts = []
    for path in discover_templates() if templates is None else templates:
        plane, role = path.parents[2].name, path.parents[1].name
        try:
            rendered = render_template(path, template_context(path, base, overrides))
        except RuntimeError as exc:
            raise AssertionError(f"{plane}/{role}/{path.name}: {exc}") from exc
        texts.append((plane, role, path.name, rendered))
    return tuple(texts)


_TEXTS: tuple[tuple[str, str, str, str], ...] | None = None


def rendered_shell_texts() -> tuple[tuple[str, str, str, str], ...]:
    """(plane, role, template name, rendered TEXT) for every `*.sh.j2` under `ansible/roles/`.

    The whole set is rendered rather than only what a caller asks for, so the render failure of
    a template no guard reads yet still arrives as a failure here — the reason
    `_k8s_render._render_all` raises too.

    Cached for the process: the render is a pure function of the repo tree, which no test
    writes to.
    """
    global _TEXTS
    if _TEXTS is None:
        base = base_context()
        texts = []
        for path in discover_templates():
            plane, role = path.parents[2].name, path.parents[1].name
            try:
                rendered = render_template(path, template_context(path, base))
            except RuntimeError as exc:
                raise AssertionError(f"{plane}/{role}/{path.name}: {exc}") from exc
            texts.append((plane, role, path.name, rendered))
        _TEXTS = tuple(texts)
    return _TEXTS


def rendered_shell_text(plane: str, role: str, template: str) -> str:
    """One entry of `rendered_shell_texts`, by (plane, role, template name).

    Reads the cache rather than rendering again, and fails naming the template when the set
    does not hold it — a renamed template is a guard over nothing, not a passing guard.
    """
    for entry in rendered_shell_texts():
        if entry[:3] == (plane, role, template):
            return entry[3]
    raise AssertionError(
        f"{plane}/{role}/{template} is not among the rendered shell templates: "
        f"{sorted(e[:3] for e in rendered_shell_texts())}"
    )


def rendered_shell_function(plane: str, role: str, template: str, name: str) -> str:
    """One shell function, `name() {` through its closing brace in column one, from the RENDERED
    script `rendered_shell_text` returns.

    The render is what makes the slice sourceable: a function body that holds a Jinja expression
    is not shell bash can run until it is expanded (#3178). Fails naming the function when the
    script does not define it, because a test that sources a function by name is a test of
    nothing once the function is gone.
    """
    script = rendered_shell_text(plane, role, template)
    match = re.search(rf"^{name}\(\) \{{[^\n]*\}}$", script, re.M) or re.search(
        # `[^\n]*` after the opening brace tolerates a trailing `# arg names` comment on the
        # same line (`push() { # up|down msg`); without it the multi-line form never matches.
        rf"^{name}\(\) \{{[^\n]*\n.*?^\}}$",
        script,
        re.M | re.S,
    )
    assert match, f"{name}() is gone from {plane}/{role}/{template}"
    return match.group(0)


def rendered_or_source_text(path: Path) -> str:
    """A `*.sh.j2` role template's RENDERED text; any other extension's SOURCE text.

    For a census that spans extensions this module does not render — a `.service.j2` unit, a
    k8s manifest, a `config.env.j2` — alongside the `*.sh.j2` templates it does (#3178).
    """
    if not path.name.endswith(".sh.j2"):
        return path.read_text()
    return rendered_shell_text(path.parents[2].name, path.parents[1].name, path.name)


def rendered_names_for(plane: str, role: str) -> set[str]:
    """Every `*.sh.j2` name rendered for one (plane, role).

    Asserts the filter still matches something: a roster derived from a (plane, role) pair that
    stopped matching comes back EMPTY, and a comparison against nothing passes.
    """
    names = {n for p, r, n, _ in rendered_shell_texts() if (p, r) == (plane, role)}
    assert names, f"no rendered shell templates for {plane}/{role}"
    return names
