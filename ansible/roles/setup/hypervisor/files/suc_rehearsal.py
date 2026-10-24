#!/usr/bin/env python3
"""The host-side decisions of the drill orchestrator's SUC rehearsal mode (H6.1b2).

Usage:
    suc_rehearsal.py pins REPO
    suc_rehearsal.py overlay RENDERED OUT HOSTNAME PLAN
    suc_rehearsal.py judge EXPECT PLAN TARGET HOSTNAME DIGEST < snapshot.json

`pins` prints the five pins the guest installs from, as `key=value` lines. REPO is a checkout:
each key is found by globbing `ansible/roles/*/*/defaults/main.yml`, so a pin that moves between
roles (H6.1b moves the k3s pins) needs no edit here, and a key held by none or by two files is an
error. The registry image is read from the `k8s/registry` role alone, because a second role copies it. `overlay` copies the system-upgrade bundle rendered at RENDERED into OUT and rewrites it
for a one-node guest (*Rehearsal in a drill guest*, docs/target-architecture/host/h6-k3s-suc.md),
then prints `target_version=<the Plan's version>`. `judge` reads one snapshot of the guest and
prints `PASS ...` (exit 0) or `WAIT ...` (exit 1) with the reason.

The orchestrator calls this under `python3 -I`; PyYAML is the only import outside the stdlib.
"""

import glob
import json
import os
import shutil
import sys

import yaml

HOSTNAME_KEY = "kubernetes.io/hostname"
KUSTOMIZATION = "system-upgrade"
FLUX_RELEASE = (
    "https://github.com/fluxcd/flux2/releases/download/{v}/flux_{n}_linux_amd64.tar.gz"
)
INSTALLER_VAR = "{{ k3s_version }}"


class Refusal(Exception):
    """The bundle or the checkout cannot support a rehearsal that proves anything."""


def _read(path):
    with open(path) as handle:
        return handle.read()


def _defaults(repo, key, role="*/*"):
    """The one `ansible/roles/*/*/defaults/main.yml` value for `key`; none or two is a refusal."""
    found = {}
    for path in sorted(
        glob.glob(os.path.join(repo, "ansible/roles", role, "defaults/main.yml"))
    ):
        with open(path) as handle:
            data = yaml.safe_load(handle) or {}
        if key in data:
            found[path] = data[key]
    if len(found) != 1:
        raise Refusal(
            f"{key} is held by {len(found)} defaults files under {repo}: {sorted(found)}"
        )
    return next(iter(found.values()))


def pins(repo):
    """The k3s installer (version, URL, sha256), the registry image and the flux CLI tarball."""
    version = _defaults(repo, "k3s_version")
    url = _defaults(repo, "k3s_install_script_url").replace(INSTALLER_VAR, version)
    if "{{" in url:
        raise Refusal(
            f"the installer URL still holds a template after substituting k3s_version: {url}"
        )
    flux = _defaults(repo, "flux_version")
    if _defaults(repo, "claude_code_takeover_check_flux_version") != flux:
        raise Refusal(
            "the flux CLI sha256 pin belongs to a release other than flux_version"
        )
    return {
        "k3s_version": version,
        "installer_url": url,
        "installer_sha256": _defaults(repo, "k3s_install_script_sha256"),
        "registry_image": _defaults(repo, "registry_k8s_image", "k8s/registry"),
        "flux_url": FLUX_RELEASE.format(v=flux, n=flux.removeprefix("v")),
        "flux_sha256": _defaults(repo, "claude_code_takeover_check_flux_sha256"),
    }


def _select_guest(plan, hostname):
    """Point the Plan's hostname clause at the guest and drop `prepare`; refuse a Plan without one."""
    clauses = plan["spec"].get("nodeSelector", {}).get("matchExpressions", [])
    hostname_clauses = [c for c in clauses if c.get("key") == HOSTNAME_KEY]
    if not hostname_clauses:
        raise Refusal(
            f"Plan {plan['metadata']['name']} has no {HOSTNAME_KEY} clause, so the overlay would select no node"
        )
    for clause in hostname_clauses:
        clause["values"] = [hostname]
    plan["spec"].pop("prepare", None)


def overlay(rendered, out, hostname, plan_name):
    """Write OUT/stage for the guest: the bundle, the Flux components and the Kustomization."""
    source = os.path.join(rendered, "cluster", KUSTOMIZATION)
    flux_file = os.path.join(rendered, "flux", f"{KUSTOMIZATION}.yaml")
    components = os.path.join(rendered, "cluster", "flux", "components.yaml")
    for needed in (source, flux_file, components):
        if not os.path.exists(needed):
            raise Refusal(
                f"{needed} is missing: render with --role flux --role {KUSTOMIZATION}"
            )
    bundle = os.path.join(out, "bundle", "cluster", KUSTOMIZATION)
    shutil.copytree(source, bundle)

    plans = {}
    for name in sorted(os.listdir(bundle)):
        path = os.path.join(bundle, name)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        if "${" in text:
            raise Refusal(
                f"{name} holds a ${{...}} placeholder, and the guest has no cluster-vars to fill it"
            )
        if not name.endswith((".yaml", ".yml")):
            continue
        docs = list(yaml.safe_load_all(text))
        found = [d for d in docs if isinstance(d, dict) and d.get("kind") == "Plan"]
        for doc in found:
            plans[doc["metadata"]["name"]] = doc
        if plan_name in {d["metadata"]["name"] for d in found}:
            _select_guest(
                next(d for d in found if d["metadata"]["name"] == plan_name), hostname
            )
            with open(path, "w") as handle:
                yaml.safe_dump_all(docs, handle, sort_keys=False)
    if plan_name not in plans:
        raise Refusal(
            f"no Plan named {plan_name} in the bundle; it holds {sorted(plans)}"
        )

    docs = [
        d
        for d in yaml.safe_load_all(_read(flux_file))
        if d and d.get("kind") == "Kustomization"
    ]
    kustomization = next(
        (d for d in docs if d["metadata"]["name"] == KUSTOMIZATION), None
    )
    if kustomization is None or not kustomization["spec"].get("healthCheckExprs"):
        raise Refusal(
            f"flux/{KUSTOMIZATION}.yaml carries no healthCheckExprs, so there is no health to judge"
        )
    if kustomization["spec"]["sourceRef"]["name"] != "homelab":
        raise Refusal(
            "the Kustomization does not follow the OCIRepository `homelab` the guest creates"
        )
    # The guest holds no other tag, no cluster-vars and no SOPS key, so these three cannot resolve.
    for field in ("dependsOn", "postBuild", "decryption"):
        kustomization["spec"].pop(field, None)
    hand = os.path.join(out, "hand")
    os.makedirs(hand)
    with open(os.path.join(hand, "kustomization.yaml"), "w") as handle:
        yaml.safe_dump(kustomization, handle, sort_keys=False)
    shutil.copy(components, os.path.join(hand, "components.yaml"))
    return plans[plan_name]["spec"]["version"]


def _condition(obj, kind):
    conditions = (obj or {}).get("status", {}).get("conditions", [])
    return next((c for c in conditions if c.get("type") == kind), {})


def judge(expect, plan_name, target, hostname, digest, snap):
    """(passed, reason) for one snapshot of the guest: {"kustomization", "plan", "node"}."""
    ks, plan, node = snap.get("kustomization"), snap.get("plan"), snap.get("node")
    ready = _condition(ks, "Ready")
    revision = (ks or {}).get("status", {}).get("lastAttemptedRevision", "")
    observed = f"Ready={ready.get('status')} reason={ready.get('reason')} message={ready.get('message')!r}"
    if not ks or not revision.endswith(digest):
        return (
            False,
            f"the Kustomization has not applied the pushed revision {digest} ({observed})",
        )
    if not plan:
        return False, f"Plan {plan_name} does not exist in the guest ({observed})"
    complete = _condition(plan, "Complete")
    if expect == "red":
        if ready.get("status") == "True":
            return False, f"the Kustomization reads Ready ({observed})"
        if (complete.get("status"), complete.get("reason")) != ("False", "JobFailed"):
            return (
                False,
                f"the Plan has not failed its Job: Complete={complete} ({observed})",
            )
        return True, f"the Plan failed and the Kustomization is red: {observed}"
    label = (
        (node or {})
        .get("metadata", {})
        .get("labels", {})
        .get(f"plan.upgrade.cattle.io/{plan_name}")
    )
    latest = plan.get("status", {}).get("latestHash")
    kubelet = (node or {}).get("status", {}).get("nodeInfo", {}).get("kubeletVersion")
    if not (latest and label == latest):
        return False, f"node label {label!r} is not the Plan's latestHash {latest!r}"
    if kubelet != target:
        return False, f"kubelet {kubelet!r} is not the Plan's version {target!r}"
    if ready.get("status") != "True":
        return False, f"the Kustomization does not read Ready ({observed})"
    return (
        True,
        f"node {hostname} runs {kubelet}, labelled {label}, and the Kustomization is Ready",
    )


def main(argv):
    try:
        if argv[:1] == ["pins"] and len(argv) == 2:
            print("\n".join(f"{k}={v}" for k, v in pins(argv[1]).items()))
        elif argv[:1] == ["overlay"] and len(argv) == 5:
            print(f"target_version={overlay(*argv[1:])}")
        elif argv[:1] == ["judge"] and len(argv) == 6:
            ok, why = judge(*argv[1:], json.load(sys.stdin))
            print(("PASS " if ok else "WAIT ") + why)
            return 0 if ok else 1
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except Refusal as err:
        print(f"suc_rehearsal: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
