"""Every Deployment and DaemonSet template takes its pod-spec shell from the shared macros.

`ansible/templates/workload-shell.yml.j2` carries the fields every long-running workload here
shares: `spec_shell` (`revisionHistoryLimit`, `strategy`) at a Deployment's `spec:` depth and
`pod_shell` (`enableServiceLinks`, `priorityClassName`, `serviceAccountName`,
`automountServiceAccountToken`, the pod-level `securityContext`) at `spec.template.spec:`
depth. A copy is where a field drifts, and a template copied from a
sibling that predates a field inherits the omission with nothing to notice.

A macro cannot force its own call, so this guard is textual and has two halves per document:

  * the call is present — `{{ pod_shell(` in every Deployment and DaemonSet document,
    `{{ spec_shell(` in every Deployment document. A DaemonSet has no ReplicaSets and no
    `strategy`, so it does not call `spec_shell`.
  * no owned field is written out by hand — the literal key at the macro's fixed depth
    (2 spaces for the spec fields, 6 for the pod fields, 8 for the pod securityContext keys
    the macro owns). A pod `securityContext` carrying something the macro does not model
    (sysctls, supplementalGroups) stays literal at the call site and passes, because none of
    its keys are the owned four.

The walk covers `ansible/templates/` as well as the roles. A shared macro that emits a whole
Deployment body — `arr-deployment.yml.j2`, radarr's and sonarr's — carries the two calls on its
callers' behalf, so the file to scan is the macro and not either caller. That is the same move
pihole's two-Deployment macro already forced, one directory further out.

Jobs and CronJobs are out of scope: their pod spec sits at another depth for a CronJob and
carries no priority tier for either (`test_pod_template_hygiene.py` says why), so a literal in
a Job document is not an offence. The rendered-fleet census in `test_pod_template_hygiene.py`,
`test_deploy_strategy.py` and `test_rendered_properties.py`'s revision-history and pod-spec rows is
the other half —
it tells an omitted call from a present one, which a grep cannot.

Run: uv run pytest ansible/tests/k8s/test_workload_shell_uses_the_macros.py
"""

import re
from pathlib import Path

from _helpers import K8S_ROLES
from lib.repo_paths import SHARED_TPL

_KIND = re.compile(r"^kind: (\w+)\s*$", re.MULTILINE)
_SPEC_LITERAL = re.compile(r"^  (revisionHistoryLimit|strategy):", re.MULTILINE)
_POD_LITERAL = re.compile(
    r"^      (enableServiceLinks|automountServiceAccountToken|priorityClassName"
    r"|serviceAccountName):",
    re.MULTILINE,
)
_POD_SC_LITERAL = re.compile(
    r"^        (runAsUser|runAsGroup|fsGroup|runAsNonRoot):", re.MULTILINE
)

# Templates the census must contain, so a missing member is named rather than counted. Both
# kinds, and the two-Deployment template (pihole), which a per-file scan would count once.
_MUST_CONTAIN = frozenset(
    {
        "authelia/deployment.yaml.j2",
        "traefik/deployment.yaml.j2",
        # pihole renders both its Deployments from one macro body, and this census reads
        # `kind:` out of the template text rather than a render — so the file to scan is the
        # macro, not either caller.
        "pihole/pihole-deployment.yaml.j2",
        "observability/prometheus.yaml.j2",
        "node-exporter/daemonset.yaml.j2",
        "dri-device-plugin/daemonset.yaml.j2",
        # radarr's and sonarr's whole Deployment body, shared out of ansible/templates/ — the
        # roles' own templates are a single macro call and name no kind.
        "templates/arr-deployment.yml.j2",
    }
)
_MIN_DOCUMENTS = 60


def _documents(text: str):
    """(kind, document text) for each `---`-separated document that names a kind."""
    for doc in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        if m := _KIND.search(doc):
            yield m.group(1), doc


def shell_offences(text: str) -> list[str]:
    """One line per Deployment/DaemonSet document that copies the shell or skips a macro."""
    offences = []
    for kind, doc in _documents(text):
        if kind not in {"Deployment", "DaemonSet"}:
            continue
        name = re.search(r"^  name: (.+)$", doc, re.MULTILINE)
        label = f"{kind}/{name.group(1).strip() if name else '<unnamed>'}"
        if "{{ pod_shell(" not in doc:
            offences.append(f"{label}: no pod_shell() call")
        if kind == "Deployment" and "{{ spec_shell(" not in doc:
            offences.append(f"{label}: no spec_shell() call")
        for pattern in (_POD_LITERAL, _POD_SC_LITERAL) + (
            (_SPEC_LITERAL,) if kind == "Deployment" else ()
        ):
            for m in pattern.finditer(doc):
                offences.append(
                    f"{label}: `{m.group(1)}:` written out instead of the macro"
                )
    return offences


def _scanned_templates() -> list[tuple[str, Path]]:
    """(label, path) for every template that can carry a workload document.

    Both trees, because a whole-Deployment macro in `ansible/templates/` is where its callers'
    documents live. A shared template that emits another kind (`service.yml.j2`, `claim-default.yaml.j2`)
    names no `kind: Deployment` and so contributes nothing here.
    """
    roles = [
        (f"{t.parent.parent.name}/{t.name}", t)
        for t in sorted(K8S_ROLES.glob("*/templates/*.yaml.j2"))
    ]
    shared = [(f"templates/{t.name}", t) for t in sorted(SHARED_TPL.glob("*.yml.j2"))]
    return roles + shared


def test_every_workload_template_takes_its_shell_from_the_macros():
    seen, count, offenders = set(), 0, {}
    for rel, template in _scanned_templates():
        text = template.read_text()
        n = sum(
            1 for kind, _ in _documents(text) if kind in {"Deployment", "DaemonSet"}
        )
        if n:
            seen.add(rel)
            count += n
        if bad := shell_offences(text):
            offenders[rel] = bad
    assert _MUST_CONTAIN <= seen, sorted(_MUST_CONTAIN - seen)
    assert count >= _MIN_DOCUMENTS, f"only {count} workload documents scanned"
    assert offenders == {}, (
        "pod-spec shell written out instead of spec_shell()/pod_shell() "
        f"(ansible/templates/workload-shell.yml.j2): {offenders}"
    )


_CLEAN = """\
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: x
spec:
{{ spec_shell('Recreate') }}
  template:
    spec:
{{ pod_shell('homelab-best-effort') }}
      securityContext:
        sysctls:
          - name: net.ipv4.ip_forward
            value: "1"
      containers:
        - name: x
          securityContext:
            runAsUser: 1000
---
apiVersion: v1
kind: Service
"""

_DAEMONSET = """\
---
kind: DaemonSet
metadata:
  name: d
spec:
  revisionHistoryLimit: 3
  template:
    spec:
{{ pod_shell('homelab-best-effort') }}
"""

_JOB = """\
---
kind: Job
metadata:
  name: j
spec:
  template:
    spec:
      enableServiceLinks: false
      priorityClassName: homelab-best-effort
"""


def test_a_document_that_calls_both_macros_is_clean():
    assert shell_offences(_CLEAN) == []
    assert (
        shell_offences(_DAEMONSET) == []
    )  # revisionHistoryLimit is not owned for a DaemonSet
    assert shell_offences(_JOB) == []  # out of scope


def test_a_copied_field_or_a_missing_call_is_flagged():
    copied = _CLEAN.replace(
        "{{ pod_shell('homelab-best-effort') }}\n", "      enableServiceLinks: false\n"
    )
    assert shell_offences(copied) == [
        "Deployment/x: no pod_shell() call",
        "Deployment/x: `enableServiceLinks:` written out instead of the macro",
    ]
    no_spec = _CLEAN.replace(
        "{{ spec_shell('Recreate') }}\n", "  revisionHistoryLimit: 3\n"
    )
    assert shell_offences(no_spec) == [
        "Deployment/x: no spec_shell() call",
        "Deployment/x: `revisionHistoryLimit:` written out instead of the macro",
    ]
    pod_sc = _CLEAN.replace(
        "        sysctls:\n", "        fsGroup: 1000\n        sysctls:\n"
    )
    assert shell_offences(pod_sc) == [
        "Deployment/x: `fsGroup:` written out instead of the macro",
    ]
