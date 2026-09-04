#!/usr/bin/env python3
"""What one whole-tree run of the k8s manifest validator must report.

`main()` renders every k8s role once — ~7s — and six guards below read different parts of that
single run: unresolved claimNames, schema failures, a role default shadowing the inventory, an
https route with no `tls:`, a NetworkPolicy on a Service's port, and the coverage tail naming
any object no schema checked.

The unit tests for the pieces `main()` calls live beside those pieces, under
`scripts/lib/tests/` — `test_k8s_yaml.py`, `test_k8s_pvc.py`, `test_k8s_schema.py`,
`test_k8s_context.py` and `test_k8s_net_rules.py`. They were split out of this file on
2026-09-04 with the code they cover. What stays here is what needs the real tree.

Run: uv run pytest scripts/validate/tests/test_validate_k8s_manifests.py
"""

import contextlib
import io

import pytest

from lib import k8s_schema
from validate import k8s_manifests as vkm


@pytest.fixture(scope="module")
def real_tree():
    """Run the validator over the real tree ONCE per worker: (exit code, stdout, stderr).

    A full run renders every k8s role and costs ~7s (2026-09-01; ~3.8s when this was written).
    Six guards below assert on different parts of the same run's output, and until 2026-09-01
    two fixtures each paid for their own run — one capturing stderr, one stdout — so every
    worker rendered the tree twice. The run is a pure function of the repo tree, so one
    capture of both streams serves all of them.

    # DECIDED: redirect_stderr/redirect_stdout, not capsys. capsys is function-scoped and
    # pytest refuses to inject it into a module-scoped fixture; main() writes with
    # print(file=sys.stderr), which redirect_stderr captures because it rebinds sys.stderr at
    # call time.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = vkm.main()
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def real_tree_run(real_tree):
    """(exit code, stderr) of the shared run — the shape the stderr guards read."""
    rc, _out, err = real_tree
    return rc, err


def test_real_tree_has_no_unresolved_claim_name(real_tree_run):
    # The actual regression guard: every claimName across the real k8s roles must resolve
    # against a rendered PVC (or a volume-claim-backed one) — a brand-new service naming a PVC
    # that was never wired up must show here, since nothing else in the tree checks this.
    rc, err = real_tree_run
    assert rc == 0
    assert "matches no rendered PersistentVolumeClaim" not in err


def test_real_tree_passes_the_schema(real_tree_run):
    # The regression guard: no rendered object in the tree fails its schema. Paired with
    # main()'s own exit code so a new failure cannot hide behind a passing older check.
    rc, err = real_tree_run
    assert rc == 0
    assert "fails the v" not in err


def test_no_real_role_shadows_an_inventory_key(real_tree_run):
    """The regression guard, over the real tree: 54 roles, zero collisions when this landed."""
    _rc, err = real_tree_run
    assert "redefines inventory key" not in err


def test_every_real_https_route_declares_tls(real_tree_run):
    _rc, err = real_tree_run
    assert "declares no spec.tls" not in err


def test_the_real_tree_has_no_netpol_port_mismatch(real_tree_run):
    _rc, err = real_tree_run
    assert "is a Service's published port" not in err


@pytest.fixture(scope="module")
def real_tree_stdout(real_tree):
    """(exit code, stdout) of the shared run — the coverage tail is printed there, not stderr."""
    rc, out, _err = real_tree
    return rc, out


_UNCOVERED = "matched neither the v"


def test_the_real_tree_leaves_no_object_unchecked(real_tree_stdout):
    """Coverage, enforced rather than observed.

    This is the staleness answer for the vendored schemas: a new CRD kind — or a rename that
    stops an existing schema matching — makes the validator report the object as uncovered, and
    this fails. Refresh or add one with scripts/validate/refresh_crd_schemas.py.
    """
    rc, out = real_tree_stdout
    assert rc == 0
    assert _UNCOVERED not in out, out[out.find(_UNCOVERED) - 80 :][:400]


def test_an_object_no_vendored_schema_covers_is_flagged(monkeypatch, tmp_path):
    """The rejecting half of the test above: with no vendored schemas, the tail must appear.

    Without this, a guard asserting the ABSENCE of a phrase would pass identically if the
    phrase were never printable — the failure mode test_every_validator_has_a_red_proof exists
    for, applied to a coverage claim instead of a validation one. It is also this suite's red
    proof for `main()` itself, which is why it carries the `..._is_flagged` name that guard's
    convention asks for.

    `CRD_SCHEMA_DIR` is patched on `lib.k8s_schema`, the module that reads it. Patching the
    facade's re-export would rebind a name nothing looks at, and the assertions below would
    fail — which is the good failure: a dead patch cannot pass here.
    """
    monkeypatch.setattr(k8s_schema, "CRD_SCHEMA_DIR", tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        vkm.main()
    out = buf.getvalue()
    assert _UNCOVERED in out
    assert "traefik.io/v1alpha1/IngressRoute" in out
