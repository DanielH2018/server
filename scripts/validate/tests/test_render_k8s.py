#!/usr/bin/env python3
"""What one whole-tree run of the k8s family of `render_templates.py` must report.

`run()` validates every k8s role once, reading each role's render off the run's shared k8s render
(`_k8s_render.rendered_tags()`), and five guards below read different parts of that
single run: unresolved claimNames, schema failures, an
https route with no `tls:`, a NetworkPolicy on a Service's port, and the coverage tail naming
any object no schema checked.

The unit tests for the pieces `run()` calls live beside those pieces: `test_k8s_yaml.py`
and `test_k8s_context.py` under `scripts/lib/tests/`, and `test_k8s_schema.py`
and `test_k8s_net_rules.py` in this directory. What stays here is what needs the real tree.

Run: uv run pytest scripts/validate/tests/test_render_k8s.py
"""

import contextlib
import io

import pytest

import _k8s_render
from renderer.renderer_lib import cluster
from renderer.renderer_lib.bundle import KUSTOMIZATION
from renderer.shadow_diff import stub_vars
from validate.validate_lib import k8s_schema
from validate.render_lib import k8s as v


def _drop(_line: str) -> None:
    """The `[ok]` printer of a run without `-v`."""


class SharedRender:
    """A `render` for `run()` that reads each role off the run's shared k8s render.

    `_k8s_render` readers on the same leg build that render anyway, so `run()` renders
    nothing a second time. A tree that fails to render leaves no shared tags, and each role
    renders afresh, so `run()` still prints its own failure lines.

    Attributes:
        asked: Each role `run()` asked for, in order.
        fresh: Each role this rendered itself because the shared render did not hold it.
    """

    def __init__(self, tags: dict[str, cluster.RenderedTag]):
        self._tags = tags
        self.asked: list[str] = []
        self.fresh: list[str] = []

    def __call__(self, role: str, stubs: dict) -> cluster.RenderedTag:
        self.asked.append(role)
        if role in self._tags:
            return self._tags[role]
        self.fresh.append(role)
        return cluster.render_tag(role, stubs)


@pytest.fixture(scope="module")
def shared_render() -> SharedRender:
    tags = {}
    with contextlib.suppress(cluster.RenderError):
        tags = _k8s_render.rendered_tags()
    return SharedRender(tags)


@pytest.fixture(scope="module")
def real_tree(shared_render):
    """Run the family over the real tree ONCE per worker: (failures, stdout, stderr).

    A full run schema-checks every rendered object, which takes several seconds.
    Six guards below assert on different parts of the same run's output. The run is a pure
    function of the repo tree, so one capture of both streams serves all of them.

    # DECIDED: redirect_stderr/redirect_stdout, not capsys. capsys is function-scoped and
    # pytest refuses to inject it into a module-scoped fixture; run() writes with
    # print(file=sys.stderr), which redirect_stderr captures because it rebinds sys.stderr at
    # call time.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        _checked, failures = v.run(_drop, render=shared_render)
    return failures, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def real_tree_run(real_tree):
    """(failures, stderr) of the shared run — the shape the stderr guards read."""
    failures, _out, err = real_tree
    return failures, err


def test_real_tree_has_no_unresolved_claim_name(real_tree_run):
    # The actual regression guard: every claimName across the real k8s roles must resolve
    # against a rendered PVC (or a volume-claim-backed one) — a brand-new service naming a PVC
    # that was never wired up must show here, since nothing else in the tree checks this.
    failures, err = real_tree_run
    assert failures == 0
    assert "matches no rendered PersistentVolumeClaim" not in err


def test_real_tree_passes_the_schema(real_tree_run):
    # The regression guard: no rendered object in the tree fails its schema. Paired with
    # run()'s own failure count so a new failure cannot hide behind a passing older check.
    failures, err = real_tree_run
    assert failures == 0
    assert "fails the v" not in err


def test_every_real_https_route_declares_tls(real_tree_run):
    _failures, err = real_tree_run
    assert "declares no spec.tls" not in err


def test_the_real_tree_has_no_netpol_port_mismatch(real_tree_run):
    _failures, err = real_tree_run
    assert "is a Service's published port" not in err


@pytest.fixture(scope="module")
def real_tree_stdout(real_tree):
    """(failures, stdout) of the shared run — the coverage tail is printed there, not stderr."""
    failures, out, _err = real_tree
    return failures, out


_UNCOVERED = "matched neither the v"


def test_the_real_tree_leaves_no_object_unchecked(real_tree_stdout):
    """Coverage, enforced rather than observed.

    This is the staleness answer for the vendored schemas: a new CRD kind — or a rename that
    stops an existing schema matching — makes the validator report the object as uncovered, and
    this fails. Refresh or add one with scripts/validate/refresh_vendored_schemas.py.
    """
    failures, out = real_tree_stdout
    assert failures == 0
    assert _UNCOVERED not in out, out[out.find(_UNCOVERED) - 80 :][:400]


def test_an_object_no_vendored_schema_covers_is_flagged(
    monkeypatch, tmp_path, shared_render
):
    """The rejecting half of the test above: with no vendored schemas, the tail must appear.

    Without this, a guard asserting the ABSENCE of a phrase would pass identically if the
    phrase were never printable — the failure mode test_every_validator_has_a_red_proof exists
    for, applied to a coverage claim instead of a validation one. It is also this suite's red
    proof for `run()` itself, which is why it carries the `..._is_flagged` name that guard's
    convention asks for.

    `CRD_SCHEMA_DIR` is patched on `validate.validate_lib.k8s_schema`, the module that reads it.
    """
    monkeypatch.setattr(k8s_schema, "CRD_SCHEMA_DIR", tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        v.run(_drop, render=shared_render)
    out = buf.getvalue()
    assert _UNCOVERED in out
    assert "traefik.io/v1alpha1/IngressRoute" in out


def test_a_shared_tag_is_the_render_run_would_make():
    assert _k8s_render.rendered_tags()["traefik"] == cluster.render_tag(
        "traefik", stub_vars()
    )


def test_run_renders_no_role_the_shared_render_holds(real_tree, shared_render):
    assert "traefik" in shared_render.asked
    assert shared_render.fresh == []


def test_a_role_the_shared_render_lacks_is_rendered_fresh_and_flagged():
    render = SharedRender({})
    assert render("traefik", stub_vars()).tag == "traefik"
    assert render.fresh == ["traefik"]


def _run_with_render(render) -> tuple[int, str]:
    """`run()` over the real roles: (failures, stderr), with `render` as the renderer."""
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        _checked, failures = v.run(_drop, render=render)
    return failures, err.getvalue()


def test_a_tag_the_renderer_refuses_is_flagged():
    def refuse(tag, stubs):
        raise cluster.RenderError(f"{tag}: refused")

    failures, err = _run_with_render(refuse)
    assert failures
    assert "[FAIL] traefik: traefik: refused" in err


@pytest.mark.parametrize("field", ["files", "deferred", "verify", "precheck"])
def test_invalid_yaml_in_any_rendered_directory_is_flagged(field):
    written = {"files": {}, field: {"bad.yaml": "kind: [\n"}}

    def render(tag, stubs):
        return cluster.RenderedTag(tag, **written)

    failures, err = _run_with_render(render)
    assert failures
    assert "[FAIL] traefik/bad.yaml" in err


def test_a_directory_s_kustomization_is_not_validated_as_a_manifest_is_clean():
    def render(tag, stubs):
        return cluster.RenderedTag(tag, {KUSTOMIZATION: "kind: [\n"})

    failures, err = _run_with_render(render)
    assert failures == 0, err


def _ghost_and_real(tmp_path):
    """A retired role's `__pycache__`-only shell beside a real role."""
    pycache = tmp_path / "retired" / "files" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "check.cpython-312.pyc").write_bytes(b"\x00")
    (tmp_path / "real" / "defaults").mkdir(parents=True)
    (tmp_path / "real" / "defaults" / "main.yml").write_text("---\n")
    return tmp_path


def test_a_role_with_defaults_is_rendered_is_clean(tmp_path):
    assert "real" in v.role_names(_ghost_and_real(tmp_path))


def test_a_pycache_only_directory_is_flagged_as_no_role(tmp_path):
    """A retired role's leftover shell renders nothing and proves nothing."""
    assert v.role_names(_ghost_and_real(tmp_path)) == ["real"]
