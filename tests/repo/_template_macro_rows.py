"""Three census rows over the k8s templates' calls of the shared securityContext and shell macros.

`test_census_rows_roles.py` runs them with its own rows, from a module of their own so that
file stays under its module-length cap. Each rule is textual on purpose: a macro cannot force
its own call, and a render cannot tell a macro call from a copy that still matches it.
"""

import re

from _row_table import Census, Subject, tracked

_ROLE_TEMPLATES = "k8s/roles/*/templates/*.yaml.j2"

# ── A container securityContext comes from the macro ────────────────────────────────────

# Pod-template depth, then CronJob `jobTemplate` depth.
_LITERAL_KEY = re.compile(r"^( {12}| {16})allowPrivilegeEscalation:", re.MULTILINE)
_ROOT = re.compile(r"^( {12}| {16})runAsUser: 0\s*$", re.MULTILINE)


def _inside(line: str, depth: int) -> bool:
    return line.startswith(" " * (depth - 1)) or not line.strip()


def _hand_written_blocks(subject: Subject) -> list[str]:
    """Each literal `allowPrivilegeEscalation:` block that does not set uid 0, by line.

    A block is the run of lines around the key indented at least as deep as the key; the
    next line at the `securityContext:` depth or shallower ends it in either direction.
    """
    lines = subject.text.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if not (key := _LITERAL_KEY.match(line)):
            continue
        depth = len(key.group(1))
        start = i
        while start > 0 and _inside(lines[start - 1], depth):
            start -= 1
        end = i + 1
        while end < len(lines) and _inside(lines[end], depth):
            end += 1
        if not _ROOT.search("\n".join(lines[start:end])):
            offenders.append(
                f"line {i + 1}: securityContext written out instead of "
                "hardened_security_context()"
            )
    return offenders


_ROOT_BLOCK = """
          securityContext:
            allowPrivilegeEscalation: false
            runAsUser: 0
            capabilities:
              drop: [ALL]
              add: [DAC_READ_SEARCH]
          volumeMounts:
"""

_COPIED_BLOCK = """
          securityContext:
            allowPrivilegeEscalation: false
            runAsNonRoot: true
            capabilities:
              drop: [ALL]
          volumeMounts:
"""


def _at_cronjob_depth(block: str) -> str:
    return block.replace("\n ", "\n     ")


_CONTAINER_SECURITY_CONTEXT_USES_THE_MACRO = Census(
    name="container-security-context-comes-from-the-macro",
    reason=(
        "`k8s/templates/security-context.yml.j2` carries the `allowPrivilegeEscalation: false` "
        "+ `capabilities.drop: [ALL]` body the container specs share, and a hand-written copy "
        "is where it drifts. The rendered-result check cannot tell a macro call from a copy "
        "that still matches. A literal `allowPrivilegeEscalation:` line, at 12 spaces (under "
        "`hardened_security_context()`) or 16 (under `job_hardened_security_context()`, a "
        "CronJob's depth), must sit in a block that sets `runAsUser: 0`: the macro refuses "
        "uid 0, so each root site keeps the comment saying which uid owns the files and why."
    ),
    files=lambda: tracked(_ROLE_TEMPLATES),
    offence=_hand_written_blocks,
    red=(
        Subject("a.yaml.j2", _COPIED_BLOCK),
        Subject("a.yaml.j2", _at_cronjob_depth(_COPIED_BLOCK)),
        Subject("a.yaml.j2", _ROOT_BLOCK + _COPIED_BLOCK),
    ),
    green=(
        Subject("a.yaml.j2", _ROOT_BLOCK),
        Subject("a.yaml.j2", _at_cronjob_depth(_ROOT_BLOCK)),
    ),
    # A file holding a literal root block is a member, so the row proves it still finds one.
    count=lambda s: int(bool(_LITERAL_KEY.search(s.text) and _ROOT.search(s.text))),
    min_matches=2,
    must_find=frozenset(
        {
            "k8s/roles/code-server/templates/deployment.yaml.j2",
            "k8s/roles/loki-homelab/templates/alloy-daemonset.yaml.j2",
        }
    ),
)

# ── A root securityContext is written out, never built by the macro ─────────────────────

# `run_as_user=0` anywhere in a hardened_security_context() call.
_ROOT_CALL = re.compile(
    r"hardened_security_context\s*\([^)]*\brun_as_user\s*=\s*0\b", re.DOTALL
)
_MACRO_CALL = re.compile(r"hardened_security_context\s*\(")


def _root_via_macro(subject: Subject) -> list[str]:
    return [
        f"line {subject.text[: m.start()].count(chr(10)) + 1}: a root securityContext "
        "built by hardened_security_context(); write it out in full"
        for m in _ROOT_CALL.finditer(subject.text)
    ]


_ROOT_SECURITY_CONTEXT_IS_WRITTEN_OUT = Census(
    name="root-security-context-is-written-out-in-full",
    reason=(
        "`k8s/templates/security-context.yml.j2` is not for `runAsUser: 0`. Each root site "
        "needs a DAC capability for a reason only its own comment can state (which uid owns "
        "the files, at what mode, and why root at all), and a macro call has nowhere to put "
        "it. The capability list stays literal so a grep for `DAC_OVERRIDE` finds every "
        "container granted it. This is about readability, not admission: the DAC census "
        "reads the render. With container-security-context-comes-from-the-macro it says a "
        "root block is written out and nothing else is."
    ),
    files=lambda: tracked("k8s/roles/*/templates/*.j2", "k8s/templates/*.j2"),
    offence=_root_via_macro,
    red=(
        Subject(
            "a.j2",
            "{{ hardened_security_context(run_as_user=0, add=['DAC_OVERRIDE']) }}",
        ),
        Subject(
            "a.j2",
            "spec:\n{{ hardened_security_context(non_root=false, run_as_user=0) }}",
        ),
    ),
    green=(
        Subject("a.j2", "{{ hardened_security_context(read_only=true) }}"),
        Subject("a.j2", "{{ hardened_security_context(run_as_user=1000) }}"),
        Subject("a.j2", "{{ hardened_security_context(run_as_user=65534) }}"),
    ),
    # Macro call sites, not files: the rule expects no offender, so a renamed macro or a
    # moved glob would otherwise read as a clean tree.
    count=lambda s: len(_MACRO_CALL.findall(s.text)),
    min_matches=50,
    must_find=frozenset(
        {
            "k8s/roles/code-server/templates/deployment.yaml.j2",
            "k8s/templates/crowdsec-agent.yml.j2",
        }
    ),
)

# ── Every Deployment and DaemonSet takes its pod-spec shell from the macros ─────────────

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
_WORKLOADS = frozenset({"Deployment", "DaemonSet"})


def _workload_documents(text: str) -> list[tuple[str, str]]:
    """(kind, document text) for each `---`-separated Deployment or DaemonSet document."""
    return [
        (m.group(1), doc)
        for doc in re.split(r"^---\s*$", text, flags=re.MULTILINE)
        if (m := _KIND.search(doc)) and m.group(1) in _WORKLOADS
    ]


def _shell_offences(subject: Subject) -> list[str]:
    offences = []
    for kind, doc in _workload_documents(subject.text):
        name = re.search(r"^  name: (.+)$", doc, re.MULTILINE)
        label = f"{kind}/{name.group(1).strip() if name else '<unnamed>'}"
        if "{{ pod_shell(" not in doc:
            offences.append(f"{label}: no pod_shell() call")
        if kind == "Deployment" and "{{ spec_shell(" not in doc:
            offences.append(f"{label}: no spec_shell() call")
        for pattern in (_POD_LITERAL, _POD_SC_LITERAL) + (
            (_SPEC_LITERAL,) if kind == "Deployment" else ()
        ):
            offences += [
                f"{label}: `{m.group(1)}:` written out instead of the macro"
                for m in pattern.finditer(doc)
            ]
    return offences


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

# revisionHistoryLimit is not owned for a DaemonSet, which has no ReplicaSets.
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

_WORKLOAD_SHELL_USES_THE_MACROS = Census(
    name="workload-shell-comes-from-the-macros",
    reason=(
        "`k8s/templates/workload-shell.yml.j2` carries the fields every long-running workload "
        "shares: `spec_shell` (`revisionHistoryLimit`, `strategy`) at a Deployment's `spec:` "
        "depth and `pod_shell` (`enableServiceLinks`, `priorityClassName`, "
        "`serviceAccountName`, `automountServiceAccountToken`, the pod `securityContext` "
        "uid keys) at `spec.template.spec:` depth. A copy is where a field drifts. Each "
        "Deployment and DaemonSet document calls `pod_shell(`, a Deployment also calls "
        "`spec_shell(`, and no owned key is written at the macro's depth. A pod "
        "`securityContext` carrying keys the macro does not model stays literal and passes. "
        "Jobs and CronJobs are out of scope. `k8s/templates/` is read too, because a macro "
        "that emits a whole Deployment body (`arr-deployment.yml.j2`) carries the calls for "
        "its callers. The rendered-fleet rows tell an omitted call from a present one."
    ),
    files=lambda: tracked(_ROLE_TEMPLATES, "k8s/templates/*.yml.j2"),
    offence=_shell_offences,
    red=(
        Subject(
            "a.yaml.j2",
            _CLEAN.replace(
                "{{ pod_shell('homelab-best-effort') }}\n",
                "      enableServiceLinks: false\n",
            ),
        ),
        Subject(
            "a.yaml.j2",
            _CLEAN.replace(
                "{{ spec_shell('Recreate') }}\n", "  revisionHistoryLimit: 3\n"
            ),
        ),
        Subject(
            "a.yaml.j2",
            _CLEAN.replace(
                "        sysctls:\n", "        fsGroup: 1000\n        sysctls:\n"
            ),
        ),
    ),
    green=(
        Subject("a.yaml.j2", _CLEAN),
        Subject("a.yaml.j2", _DAEMONSET),
        Subject("a.yaml.j2", _JOB),
    ),
    count=lambda s: len(_workload_documents(s.text)),
    min_matches=60,
    must_find=frozenset(
        {
            "k8s/roles/authelia/templates/deployment.yaml.j2",
            "k8s/roles/traefik/templates/deployment.yaml.j2",
            # pihole renders both its Deployments from one macro body, and the row reads
            # `kind:` out of the template text, so the file it scans is the macro.
            "k8s/roles/pihole/templates/pihole-deployment.yaml.j2",
            "k8s/roles/observability/templates/prometheus.yaml.j2",
            "k8s/roles/node-exporter/templates/daemonset.yaml.j2",
            "k8s/roles/dri-device-plugin/templates/daemonset.yaml.j2",
            "k8s/templates/arr-deployment.yml.j2",
        }
    ),
)

TEMPLATE_MACRO_ROWS = (
    _CONTAINER_SECURITY_CONTEXT_USES_THE_MACRO,
    _ROOT_SECURITY_CONTEXT_IS_WRITTEN_OUT,
    _WORKLOAD_SHELL_USES_THE_MACROS,
)
