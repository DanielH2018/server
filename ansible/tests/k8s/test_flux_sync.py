"""The flux role's sync objects (`templates/sync.yaml.j2`): no suspend, the drill, the signer.

- the sync render carries no `suspend` key, and the renderer refuses `flux_sync_suspend: true`
  (D5 step 29), because Flux applies these objects itself from then on;
- the `verify-drill` OCIRepository renders only with its lever on, and is no Discord source;
- the OCIRepository admits exactly the signer `promote.py` verifies, and `last-good`, the soak
  Job's base, carries the same `verify` block.

Run: uv run pytest ansible/tests/k8s/test_flux_sync.py
"""

import copy
import re
import shutil

import forge
import pytest

from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from renderer.homelab_render import secret_keys
from renderer.renderer_lib.cluster import render_tag
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.placeholders import placeholder_vars


# --- No suspend renders, and the renderer refuses the old lever ---------------------------

_SUSPENDABLE = {
    ("OCIRepository", "homelab"),
    ("OCIRepository", "last-good"),
    ("Kustomization", "homelab"),
}


def _sync_docs(**overrides) -> list[dict]:
    text = render_role_template("flux", "sync.yaml.j2", overrides)
    return [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]


def suspend_offences(docs: list[dict]) -> list[str]:
    """`<kind>/<name>` for each suspendable object that renders a `spec.suspend` key at all.

    `true` would stop Flux's own source, and an explicit `false` would take the field back
    from an operator's hand suspend at the next reconcile of `flux-deferred`. A suspendable
    object missing from the render is an offence too, so a renamed object cannot pass by
    absence.
    """
    found = {
        (d["kind"], d["metadata"]["name"]): d["spec"]
        for d in docs
        if (d["kind"], d["metadata"]["name"]) in _SUSPENDABLE
    }
    offences = [f"{k}/{n} missing" for k, n in sorted(_SUSPENDABLE - set(found))]
    offences += [
        f"{kind}/{name} suspend={spec['suspend']}"
        for (kind, name), spec in sorted(found.items())
        if "suspend" in spec
    ]
    return offences


def test_suspend_offences_is_clean_on_the_sync_render():
    assert suspend_offences(_sync_docs()) == []


def test_suspend_offences_is_flagged_on_a_rendered_suspend():
    """The red half: either value of the field is an offence."""
    docs = copy.deepcopy(_sync_docs())
    for value, doc in zip(
        (True, False, False),
        (d for d in docs if (d["kind"], d["metadata"]["name"]) in _SUSPENDABLE),
        strict=True,
    ):
        doc["spec"]["suspend"] = value
    assert suspend_offences(docs) == [
        "Kustomization/homelab suspend=False",
        "OCIRepository/homelab suspend=True",
        "OCIRepository/last-good suspend=False",
    ]


def _placeholders() -> dict[str, object]:
    """The extra vars a placeholder render lays over the context: one sentinel per secret."""
    return placeholder_vars(secret_keys())


def test_the_renderer_renders_the_flux_role_with_the_lever_off():
    assert "sync.yaml" in render_tag("flux", _placeholders()).deferred


def test_the_renderer_refuses_flux_sync_suspend_true_in_the_roles_defaults(tmp_path):
    """The red half, as a PR would write it: the role's own defaults set the key."""
    shutil.copytree(K8S_ROLES / "flux", tmp_path / "flux")
    defaults = tmp_path / "flux" / "defaults" / "main.yml"
    text = defaults.read_text()
    assert "\nflux_sync_suspend: false\n" in text
    defaults.write_text(
        text.replace("\nflux_sync_suspend: false\n", "\nflux_sync_suspend: true\n")
    )
    with pytest.raises(RenderError, match="flux_sync_suspend is true"):
        render_tag("flux", _placeholders(), roles=tmp_path)


def test_the_renderer_refuses_flux_sync_suspend_true_as_an_extra_var():
    """The string an `-e` assignment carries is read through Ansible's `bool`, as the role is."""
    with pytest.raises(RenderError, match="flux_sync_suspend is true"):
        render_tag("flux", {**_placeholders(), "flux_sync_suspend": "true"})


# --- The verify drill is a lever, and never a Discord source -----------------------------


def _names(docs: list[dict], kind: str) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d["kind"] == kind}


def test_verify_drill_renders_only_while_its_lever_is_on():
    assert _names(_sync_docs(flux_verify_drill=False), "OCIRepository") == {
        "homelab",
        "last-good",
    }
    assert _names(_sync_docs(flux_verify_drill=True), "OCIRepository") == {
        "homelab",
        "last-good",
        "verify-drill",
    }


def test_deploy_errors_names_no_drill_source():
    docs = _sync_docs(flux_verify_drill=True)
    alert = next(
        d
        for d in docs
        if d["kind"] == "Alert" and d["metadata"]["name"] == "deploy-errors"
    )
    sources = {(s["kind"], s["name"]) for s in alert["spec"]["eventSources"]}
    assert sources == {("Kustomization", "*"), ("OCIRepository", "homelab")}, sources


# --- The cluster and `promote.py` agree on what counts as signed -------------------------


def identity_admits(docs: list[dict], subject: str, issuer: str) -> bool:
    """Whether the OCIRepository `homelab` accepts a keyless signature from `subject`/`issuer`.

    source-controller matches each field as a Go regular expression; these patterns use only
    syntax that Python's `re` reads the same way.
    """
    repo = next(
        d
        for d in docs
        if d["kind"] == "OCIRepository" and d["metadata"]["name"] == "homelab"
    )
    assert repo["spec"]["verify"]["provider"] == "cosign", repo["spec"]["verify"]
    return any(
        re.search(m["subject"], subject) and re.search(m["issuer"], issuer)
        for m in repo["spec"]["verify"]["matchOIDCIdentity"]
    )


def test_last_good_verifies_as_homelab_does_and_follows_its_tag():
    """The soak Job reads `last-good`'s `digests.json` only once source-controller verified it."""
    repos = {
        d["metadata"]["name"]: d["spec"]
        for d in _sync_docs()
        if d["kind"] == "OCIRepository"
    }
    assert repos["last-good"]["verify"] == repos["homelab"]["verify"]
    assert repos["last-good"]["url"] == repos["homelab"]["url"]
    assert repos["last-good"]["ref"] == {"tag": "last-good"}


def test_identity_admits_the_signer_promote_verifies():
    assert identity_admits(_sync_docs(), forge.SIGNER_IDENTITY, forge.SIGNER_ISSUER)


@pytest.mark.parametrize(
    "subject",
    [
        forge.SIGNER_IDENTITY.replace("promote.yml", "ci.yml"),
        forge.SIGNER_IDENTITY.replace("refs/heads/master", "refs/heads/feature"),
        forge.SIGNER_IDENTITY + "x",
        forge.SIGNER_IDENTITY.replace("github.com/", "githubXcom/"),
    ],
    ids=["other-workflow", "other-ref", "suffix", "unescaped-dot"],
)
def test_identity_admits_is_flagged_on_another_signer(subject):
    assert not identity_admits(_sync_docs(), subject, forge.SIGNER_ISSUER)
