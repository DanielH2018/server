"""The three cold-start runbooks send the operator to the Flux bootstrap, not to deploy.sh (D5 step 29).

From D5 step 29 the flux tag is flux-owned, so `./scripts/deploy.sh` deploys no workload on the
cluster and installs no Flux. Each runbook's cold-start section must link
docs/flux-deploy-plane.md's *Bootstrapping Flux on a rebuilt cluster*, and a step there that
still names `./scripts/deploy.sh` must carry that link beside it. The red fixture is
docs/break-glass.md's *daniel-box is dead* scenario as it stood at `70591c307`, whose step 4
was "Deploy: `./scripts/deploy.sh`".

Run: uv run pytest ansible/tests/repo/test_cold_start_runbooks_link_bootstrap.py
"""

import pytest

from _markdown_section import section, steps
from lib.repo_paths import REPO

LINK = "flux-deploy-plane.md#bootstrapping-flux-on-a-rebuilt-cluster"
DEPLOY = "./scripts/deploy.sh"
RUNBOOKS = {
    "docs/break-glass.md": "### Scenario: daniel-box is dead, daniel-server survives",
    "docs/longhorn-disaster-recovery.md": "## Procedure (fresh host, total loss)",
    "docs/k3s-etcd-restore.md": "## After the restore",
}

# docs/break-glass.md's scenario at 70591c307, every line below its heading, as
# `git show 70591c307:docs/break-glass.md` prints it.
SCENARIO_AT_70591C307 = """
daniel-server keeps its own SOPS key, so decryption still works without the recovery key.

1. Follow `docs/longhorn-disaster-recovery.md` → *Procedure (fresh host, total loss)*,
   through *Cluster bring-up*: bring up a replacement daniel-box with `ansible/bootstrap.yml`, add its
   pubkey to `ansible/.sops.yaml`, `sops updatekeys` from daniel-server, commit, pull.
2. Decide whether cluster **objects** (Deployments, Secrets, PVC bindings) are also lost —
   if so, restore etcd first per `docs/k3s-etcd-restore.md` (needs item 2, the cluster
   token, if this is a genuinely new host rather than the same disk).
3. Restore Longhorn volumes per `docs/longhorn-disaster-recovery.md` (*Wait for the backupstore sync*, then
   *Restore volumes BEFORE any deploy*), **before** the deploy — deploying first provisions
   empty PVCs under the same names.
4. Deploy: `./scripts/deploy.sh`.
5. Verify with `probe.py targets` / `probe.py health <svc>` per that doc's *Verify* step.
"""


def cold_start_problems(text: str) -> list[str]:
    """One line per way a cold-start section still sends the operator to `deploy.sh`."""
    problems = [] if LINK in text else ["no link to the bootstrap section"]
    problems += [
        f"step {n} names {DEPLOY} without the bootstrap section's link"
        for n, step in sorted(steps(text).items())
        if DEPLOY in step and LINK not in step
    ]
    return problems


@pytest.mark.parametrize(("doc", "heading"), sorted(RUNBOOKS.items()))
def test_cold_start_problems_is_clean_on_each_runbook(doc, heading):
    text = section((REPO / doc).read_text(), heading)
    assert steps(text), f"{doc}: no numbered step under {heading!r}"
    assert cold_start_problems(text) == []


def test_the_link_names_the_bootstrap_heading():
    doc = (REPO / "docs" / "flux-deploy-plane.md").read_text()
    assert "\n## Bootstrapping Flux on a rebuilt cluster\n" in doc


def test_cold_start_problems_is_flagged_on_break_glass_at_70591c307():
    assert cold_start_problems(SCENARIO_AT_70591C307) == [
        "no link to the bootstrap section",
        f"step 4 names {DEPLOY} without the bootstrap section's link",
    ]
