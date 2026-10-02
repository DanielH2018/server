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

from pathlib import Path

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
