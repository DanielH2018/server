"""Which tasks in a task file write a cluster object: the predicate of the H1.1h write-verb census.

Two checks read it. The `k3s-role-cluster-writes-are-counted` row in
`ansible/tests/repo/_k3s_write_verbs_row.py` counts the writers per `setup/k3s` task file, and
`ansible/tests/setup/test_k3s_stale_checkout_map.py` holds each one to a row of the
stale-checkout guard's map. It sits at the `ansible/tests` root because that is the directory
both reach by bare name.
"""

import re

from _helpers import walk_tasks
from _row_table import Subject
from lib import yaml_fast

# The kubectl subcommands that change an object. `rollout` is a write only with these
# sub-verbs; `rollout status` and `rollout history` read.
_WRITE_VERBS = frozenset(
    {
        "annotate",
        "apply",
        "autoscale",
        "cordon",
        "create",
        "delete",
        "drain",
        "edit",
        "expose",
        "label",
        "patch",
        "replace",
        "run",
        "scale",
        "set",
        "taint",
        "uncordon",
    }
)
# The subcommands that write only with these sub-verbs: `rollout status`, `auth can-i` and
# `certificate --help` read.
_SUB_VERB_WRITES = {
    "rollout": frozenset({"pause", "restart", "resume", "undo"}),
    "auth": frozenset({"reconcile"}),
    "certificate": frozenset({"approve", "deny"}),
}
# The flags that take their value as the next word, so the value is not read as the verb:
# kubectl's global value flags (`kubectl options`), plus `--field-manager`, the one
# per-command value flag a write here is likely to put first. A `--flag=value` word needs no
# entry, and a boolean flag skips as one word.
_VALUE_FLAGS = frozenset(
    {
        "-n",
        "--namespace",
        "--kubeconfig",
        "--context",
        "--cluster",
        "--user",
        "-s",
        "--server",
        "--as",
        "--as-group",
        "--as-uid",
        "--cache-dir",
        "--certificate-authority",
        "--client-certificate",
        "--client-key",
        "--password",
        "--profile",
        "--profile-output",
        "--request-timeout",
        "--tls-server-name",
        "--token",
        "--username",
        "-v",
        "--v",
        "--vmodule",
        "--field-manager",
    }
)
# A word, with a Jinja expression kept whole: `-n {{ k3s_readonly_sa_namespace }}` is two words.
_WORD = re.compile(r"\S*\{\{.*?\}\}\S*|\S+")
# The kubernetes.core modules that write. `k8s_info` and `k8s_log` read.
_WRITE_MODULES = frozenset(
    {"k8s", "k8s_json_patch", "k8s_scale", "k8s_drain", "k8s_taint", "helm"}
)


def _command_text(args) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        argv = args.get("argv")
        return " ".join(map(str, argv)) if argv else str(args.get("cmd", ""))
    return ""


def kubectl_writes(command: str) -> list[str]:
    """`kubectl <verb>` for every kubectl call in `command` whose subcommand writes.

    The verb is the first word after `kubectl` that is not a global flag or a flag's value,
    so `wait --for=create`, `rollout status` and `auth can-i` read, and `set image`,
    `--request-timeout 30s apply` and `auth reconcile` write.
    """
    words = _WORD.findall(command)
    found = []
    for i, word in enumerate(words):
        if word != "kubectl" and not word.endswith("/kubectl"):
            continue
        j = i + 1
        while j < len(words) and words[j].startswith("-"):
            j += 2 if words[j] in _VALUE_FLAGS else 1
        verb = words[j] if j < len(words) else ""
        if verb in _SUB_VERB_WRITES:
            sub = words[j + 1] if j + 1 < len(words) else ""
            if sub in _SUB_VERB_WRITES[verb]:
                found.append(f"kubectl {verb} {sub}")
        elif verb in _WRITE_VERBS:
            found.append(f"kubectl {verb}")
    return found


# A `uri` call to the API server: its port, or a core or group API path.
_API_URL = re.compile(r":6443\b|/api/v1/|/apis/")


def _writes(task: dict) -> list[str]:
    hits = []
    for key, args in task.items():
        module = key.rsplit(".", 1)[-1]
        if module in ("command", "shell", "raw"):
            hits += kubectl_writes(_command_text(args))
        elif module in _WRITE_MODULES:
            hits.append(module)
        elif module == "script":
            # The script's body is a control-node file this census does not read, so it
            # counts as a writer until an allow entry or a guard row says otherwise.
            hits.append("script")
        elif module == "uri" and isinstance(args, dict):
            method = str(args.get("method", "GET")).upper()
            if method not in ("GET", "HEAD") and _API_URL.search(
                str(args.get("url", ""))
            ):
                hits.append(f"uri {method}")
    return hits


def cluster_writer_names(text: str) -> list[str]:
    """The name of every task in a task file that writes a cluster object."""
    return [
        str(task.get("name", "<unnamed>"))
        for task in walk_tasks(yaml_fast.safe_load(text) or [])
        if _writes(task)
    ]


def cluster_writes(subject: Subject) -> list[str]:
    """`<task name>: <how it writes>` for every task in `subject` that writes the cluster."""
    return [
        f"{task.get('name', '<unnamed>')}: {', '.join(hits)}"
        for task in walk_tasks(yaml_fast.safe_load(subject.text) or [])
        if (hits := _writes(task))
    ]
