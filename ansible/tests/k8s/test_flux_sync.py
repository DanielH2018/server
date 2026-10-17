"""The flux role's sync objects (`templates/sync.yaml.j2`): the suspend lever, the drill, the signer.

- `flux_sync_suspend` renders `suspend: true` or no `suspend` key, never `false`;
- the `verify-drill` OCIRepository renders only with its lever on, and is no Discord source;
- the OCIRepository admits exactly the signer `promote.py` verifies, and `last-good`, the soak
  Job's base, carries the same `verify` block.

Run: uv run pytest ansible/tests/k8s/test_flux_sync.py
"""

import copy
import re

import forge
import pytest

from _k8s_render import render_role_template
from lib import yaml_fast


# --- The suspend lever renders only `true`, never `false` -------------------------------

_SUSPENDABLE = {
    ("OCIRepository", "homelab"),
    ("OCIRepository", "last-good"),
    ("Kustomization", "homelab"),
}


def _sync_docs(**overrides) -> list[dict]:
    text = render_role_template("flux", "sync.yaml.j2", overrides)
    return [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]


def suspend_offences(docs: list[dict], lever: bool) -> list[str]:
    """`<kind>/<name>` for each suspendable object whose `spec.suspend` the lever does not explain.

    Lever on: both objects must carry `suspend: true`. Lever off: neither may carry the key at
    all, because the client-side apply would turn an explicit `false` into an undo of an
    operator's hand suspend. A suspendable object missing from the render is an offence too,
    so a renamed object cannot pass by absence.
    """
    found = {
        (d["kind"], d["metadata"]["name"]): d["spec"]
        for d in docs
        if (d["kind"], d["metadata"]["name"]) in _SUSPENDABLE
    }
    offences = [f"{k}/{n} missing" for k, n in sorted(_SUSPENDABLE - set(found))]
    for (kind, name), spec in sorted(found.items()):
        if lever and spec.get("suspend") is not True:
            offences.append(f"{kind}/{name} suspend={spec.get('suspend', '<absent>')}")
        if not lever and "suspend" in spec:
            offences.append(f"{kind}/{name} suspend={spec['suspend']}")
    return offences


def test_suspend_offences_is_clean_with_the_lever_off():
    assert suspend_offences(_sync_docs(flux_sync_suspend=False), lever=False) == []


def test_suspend_offences_is_clean_with_the_lever_on():
    assert suspend_offences(_sync_docs(flux_sync_suspend=True), lever=True) == []


def test_suspend_offences_is_flagged_on_an_explicit_false():
    """The red half: `suspend: false` with the lever off undoes a hand suspend."""
    docs = copy.deepcopy(_sync_docs(flux_sync_suspend=False))
    for doc in docs:
        if (doc["kind"], doc["metadata"]["name"]) in _SUSPENDABLE:
            doc["spec"]["suspend"] = False
    assert suspend_offences(docs, lever=False) == [
        "Kustomization/homelab suspend=False",
        "OCIRepository/homelab suspend=False",
        "OCIRepository/last-good suspend=False",
    ]


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
