"""What a job's script asks of the API server, and whether its rendered RBAC grants it.

Target slice H2 moves host crons into CronJobs, each with its own ServiceAccount whose rules are
the verbs and resources its script calls and nothing else
(`docs/target-architecture/host/h2-shrink-host-plane.md`, *The pattern for one moved cron*). This
module derives that set and checks it against rendered Roles and ClusterRoles, so a script line
added without its grant fails a test rather than a 04:10 UTC run.

- `shell_needs` reads every `$KUBECTL` line of a shell script, and the `kind:` lines of its
  heredocs for an `apply`.
- `argv_needs` reads the argv lists a Python module's recording runner captured.
- `rest_needs` reads the `(method, path)` calls a Job script made straight to the API server.
- `missing` names each need no grant covers.

A need is `(namespace, apiGroup, resource, verb, name)`. The name is the object the call names,
or None when it names none (a list, a pod a Deployment resolves to). A rule with
`resourceNames` covers only a need whose name it lists, and never `create`, which RBAC cannot
narrow by name. A name the script holds in a variable cannot be checked against such a rule,
so that comparison raises. The namespace is None for a cluster-scoped
resource and `ALL_NAMESPACES` for `-A`; both need a ClusterRole. A namespace the script holds in
a variable, such as the drill's `"$PVC_NS"`, resolves through the `namespaces` map the caller
passes, and an unresolved one raises: the caller states which namespace the variable holds.

The implied verbs are the ones kubectl makes on the script's behalf. `exec` and `logs` on a
Deployment or Job resolve it and list its pods. `apply` reads, then creates or patches. `patch`,
`label` and `annotate` read the object, then patch it. `delete`
waits for the object to go with a `list` and a `watch`, and skips the wait silently, still
exiting 0, when either is Forbidden. A verb or resource this module does not know raises, so a
new script shape extends the tables here rather than passing unread.
"""

import re
from collections.abc import Sequence
import shlex

ALL_NAMESPACES = "*"

# kubectl's spellings of a resource -> (apiGroup, plural, namespaced).
_RESOURCE_SPELLINGS = {
    ("", "pods", True): ("po", "pod", "pods"),
    ("", "persistentvolumeclaims", True): (
        "pvc",
        "persistentvolumeclaim",
        "persistentvolumeclaims",
    ),
    ("", "persistentvolumes", False): ("pv", "persistentvolume", "persistentvolumes"),
    ("", "configmaps", True): ("cm", "configmap", "configmaps"),
    ("apps", "deployments", True): (
        "deploy",
        "deployment",
        "deployments",
        "deployments.apps",
    ),
    ("batch", "jobs", True): ("job", "jobs", "jobs.batch"),
    ("batch", "cronjobs", True): ("cj", "cronjob", "cronjobs", "cronjobs.batch"),
    ("longhorn.io", "volumes", True): ("volume", "volumes", "volumes.longhorn.io"),
    ("longhorn.io", "backups", True): ("backup", "backups", "backups.longhorn.io"),
    ("longhorn.io", "backuptargets", True): (
        "backuptarget",
        "backuptargets",
        "backuptargets.longhorn.io",
    ),
    ("traefik.io", "ingressroutes", True): (
        "ingressroute",
        "ingressroutes",
        "ingressroutes.traefik.io",
    ),
}
RESOURCES = {
    spelling: resource
    for resource, spellings in _RESOURCE_SPELLINGS.items()
    for spelling in spellings
}
# A manifest's `kind:` -> the same triple, for an `apply`.
KINDS = {
    "Pod": RESOURCES["pod"],
    "PersistentVolumeClaim": RESOURCES["pvc"],
    "PersistentVolume": RESOURCES["pv"],
    "ConfigMap": RESOURCES["configmap"],
    "Job": RESOURCES["job"],
    "Volume": RESOURCES["volume"],
}
PODS = RESOURCES["pod"]

# Flags whose value is the next word. A `--flag=value` word carries its own.
_VALUE_FLAGS = frozenset(
    {
        "-n",
        "--namespace",
        "-o",
        "--output",
        "-l",
        "--selector",
        "-c",
        "--container",
        "-f",
        "--filename",
        "--image",
        "--overrides",
        "--restart",
        "--field-selector",
        "-p",
        "--patch",
        "--type",
        "--timeout",
        "--request-timeout",
        "--tail",
        "--since",
        "--from",
    }
)
# What ends the kubectl command on its line: a redirect with its descriptor (`2>`), a pipe, a
# list operator or a parenthesis. Otherwise a shell word, its quoted and bare pieces joined, so
# `jsonpath='{.a}'` stays one word.
_END = re.compile(r"\d*[<>]|[|;&()]")
_WORD = re.compile(r"""\d*[<>]|[|;&()]|(?:'[^']*'|"(?:\\.|[^"\\])*"|[^\s'"|;&<>()])+""")
_KUBECTL = re.compile(r"\$\{?KUBECTL\}?")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\s*$", re.S | re.M)


def _namespace(word: str | None, namespaces: dict[str, str]) -> str | None:
    if word is None or not word.startswith("$"):
        return word
    key = "$" + word.strip("${}")
    if key not in namespaces:
        raise KeyError(f"namespace {word} is a variable the caller did not resolve")
    return namespaces[key]


def _scope(resource: tuple, namespace: str | None) -> str | None:
    """The need's namespace: None for a cluster-scoped resource, `namespace` otherwise."""
    return namespace if resource[2] else None


def _resource(word: str) -> tuple[tuple, bool]:
    """(resource, named) for `type`, `type/name` or a bare pod name after `logs` or `exec`."""
    kind, slash, _name = word.partition("/")
    if kind not in RESOURCES:
        raise KeyError(
            f"unknown kubectl resource {kind!r}; add it to _RESOURCE_SPELLINGS"
        )
    return RESOURCES[kind], bool(slash)


def argv_needs(
    argv: list[str],
    namespaces: dict[str, str] | None = None,
    applied: Sequence[tuple[str, str | None, str | None]] = (),
) -> set[tuple]:
    """The needs of one kubectl call, `argv` starting at the word after `kubectl`.

    `applied` is `(kind, namespace, name)` for each manifest an `apply -f` sends.

    Raises:
      KeyError: a verb, resource, kind or namespace variable this module cannot resolve.
    """
    namespaces = namespaces or {}
    words, namespace, everywhere = [], None, False
    it = iter(argv)
    for word in it:
        flag = word.split("=", 1)[0]
        if flag in ("-A", "--all-namespaces"):
            everywhere = True
        elif flag in ("-n", "--namespace"):
            namespace = word.split("=", 1)[1] if "=" in word else next(it)
        elif word.startswith("-"):
            if flag in _VALUE_FLAGS and "=" not in word:
                next(it, None)
        else:
            words.append(word)
    namespace = ALL_NAMESPACES if everywhere else _namespace(namespace, namespaces)
    verb, rest = words[0], words[1:]

    def need(
        resource: tuple,
        *verbs: str,
        ns: str | None = namespace,
        name: str | None = None,
    ) -> set[tuple]:
        return {
            (_scope(resource, ns), resource[0], resource[1], v, name) for v in verbs
        }

    if verb == "apply":
        found = set()
        for kind, ns, name in applied:
            if kind not in KINDS:
                raise KeyError(f"unknown applied kind {kind!r}; add it to KINDS")
            found |= need(
                KINDS[kind],
                "get",
                "create",
                "patch",
                ns=_namespace(ns, namespaces) or namespace,
                name=name,
            )
        return found
    if verb == "run":
        return need(PODS, "create", name=rest[0])
    if verb == "create" and rest[:1] == ["job"]:
        source = next((w.split("/", 1)[1] for w in argv if "cronjob/" in w), None)
        return need(RESOURCES["job"], "create", name=rest[1]) | need(
            RESOURCES["cronjob"], "get", name=source
        )
    if verb in ("logs", "exec"):
        target = rest[0] if "/" in rest[0] else f"pod/{rest[0]}"
        resource, _ = _resource(target)
        name = target.split("/", 1)[1]
        found = need(resource, "get", name=name)
        # The pod a Deployment or Job resolves to is chosen at run time, so it has no name here.
        pod = name if resource == PODS else None
        if resource != PODS:
            found |= need(PODS, "list")
        sub = ("", "pods/log", True) if verb == "logs" else ("", "pods/exec", True)
        sub_verbs = ("get",) if verb == "logs" else ("create", "get")
        return found | need(sub, *sub_verbs, name=pod)
    if verb in ("get", "delete", "patch", "label", "annotate"):
        if verb in ("label", "annotate"):
            # The `key=value` and `key-` words after the objects name no object.
            rest = [w for w in rest if "=" not in w and not w.endswith("-")]
        # Every object the call names is a need of its own: the API server authorizes each
        # against `resourceNames` separately, so a second name the rule omits is Forbidden.
        if "/" in rest[0]:
            objects = [(_resource(w)[0], w.split("/", 1)[1]) for w in rest]
        else:
            resource, _ = _resource(rest[0])
            objects = [(resource, name) for name in rest[1:]] or [(resource, None)]
        found = set()
        for resource, name in objects:
            if verb == "get":
                found |= need(resource, "get" if name else "list", name=name)
            elif verb == "delete":
                # The wait lists and watches with a `metadata.name` field selector, which
                # RBAC authorizes against `resourceNames` like a get.
                found |= need(resource, "delete", "list", "watch", name=name)
            else:
                # `kubectl patch`, `label` and `annotate` GET the object first, then send the
                # PATCH, so the role needs both verbs on it.
                found |= need(resource, "get", "patch", name=name)
        return found
    raise KeyError(f"unknown kubectl verb {verb!r}; teach argv_needs what it calls")


def _heredoc_manifests(script: str) -> list[tuple[str, str | None, str | None]]:
    """(kind, namespace, name) for each manifest document in any heredoc of `script`."""
    found = []
    for _tag, body in _HEREDOC.findall(script):
        for doc in re.split(r"^---\s*$", body, flags=re.M):
            kind = re.search(r"^kind:\s*(\S+)", doc, re.M)
            namespace = re.search(r"^  namespace:\s*(\S+)", doc, re.M)
            name = re.search(r"^  name:\s*(\S+)", doc, re.M)
            if kind:
                found.append(
                    (
                        kind.group(1),
                        namespace and namespace.group(1),
                        name and name.group(1),
                    )
                )
    return found


def kubectl_lines(script: str) -> list[list[str]]:
    """The words after each `$KUBECTL` in `script`, continuations joined.

    A line can hold several calls (`$KUBECTL get ... | $KUBECTL apply -f -`, `A=$($KUBECTL
    ...) && $KUBECTL ...`), so every match on a line starts a call of its own.
    """
    calls = []
    for line in script.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in _KUBECTL.finditer(line):
            words = []
            for word in _WORD.findall(line[m.end() :]):
                if _END.fullmatch(word):
                    break
                words.append("".join(shlex.split(word)))
            calls.append(words)
    return calls


def shell_needs(script: str, namespaces: dict[str, str] | None = None) -> set[tuple]:
    """Every need of a shell script's `$KUBECTL` lines; an `apply` takes its heredocs' kinds."""
    applied = _heredoc_manifests(script)
    found = set()
    for words in kubectl_lines(script):
        found |= argv_needs(words, namespaces, applied)
    return found


def recorded_needs(
    argvs: list[list[str]], namespaces: dict[str, str] | None = None
) -> set[tuple]:
    """Every need of the kubectl argvs a recording runner captured, `k3s` prefix or not."""
    found = set()
    for argv in argvs:
        start = argv.index("kubectl") + 1 if "kubectl" in argv else 0
        found |= argv_needs(argv[start:], namespaces)
    return found


# The RBAC verb of each HTTP method a script sends; GET is `get` on a named object, else `list`.
_REST_VERBS = {"POST": "create", "PATCH": "patch", "PUT": "update", "DELETE": "delete"}


def rest_needs(calls: Sequence[tuple[str, str]]) -> set[tuple]:
    """Every need of the `(method, path)` calls a script made to the API server's REST paths.

    `/api/v1/...` is the core group and `/apis/<group>/<version>/...` any other. A path with no
    `namespaces/<ns>` segment is cluster-scoped, so its namespace is None.

    Raises:
      KeyError: a method this module cannot map to a verb.
    """
    found = set()
    for method, path in calls:
        parts = path.split("?", 1)[0].strip("/").split("/")
        group, rest = ("", parts[2:]) if parts[0] == "api" else (parts[1], parts[3:])
        namespace = None
        if rest[0] == "namespaces" and len(rest) > 2:
            namespace, rest = rest[1], rest[2:]
        resource, name = rest[0], rest[1] if len(rest) > 1 else None
        verb = (
            "get" if method == "GET" and name else "list" if method == "GET" else None
        )
        found.add((namespace, group, resource, verb or _REST_VERBS[method], name))
    return found


def grants(docs: list[dict], namespace: str, service_account: str) -> list[tuple]:
    """`(namespace or None, apiGroups, resources, verbs, resourceNames)` per rule bound to it.

    An empty `resourceNames` set means the rule names no object, so it covers every name.

    A RoleBinding grants its Role's or ClusterRole's rules in the binding's namespace only. A
    ClusterRoleBinding grants a ClusterRole's rules everywhere, which is the None namespace.
    """
    roles = {
        (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d.get(
            "rules"
        )
        or []
        for d in docs
        if d.get("kind") in ("Role", "ClusterRole")
    }
    found = []
    for d in docs:
        if d.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        if not any(
            s.get("kind") == "ServiceAccount"
            and s.get("name") == service_account
            and s.get("namespace", d["metadata"].get("namespace")) == namespace
            for s in d.get("subjects") or []
        ):
            continue
        scope = d["metadata"].get("namespace") if d["kind"] == "RoleBinding" else None
        ref = d["roleRef"]
        role_ns = scope if ref["kind"] == "Role" else None
        for rule in roles.get((ref["kind"], role_ns, ref["name"]), []):
            found.append(
                (
                    scope,
                    set(rule.get("apiGroups") or []),
                    set(rule.get("resources") or []),
                    set(rule.get("verbs") or []),
                    set(rule.get("resourceNames") or []),
                )
            )
    return found


def missing(needs: set[tuple], granted: list[tuple]) -> list[tuple]:
    """Each need, sorted, that no grant covers; `*` in a rule matches anything.

    Raises:
      KeyError: a need no rule covers outright has a shell-variable name and meets a rule with
        `resourceNames`, so whether that rule covers it depends on a value picked at run time.
    """

    def by_name(names: set[str], verb: str, name: str | None) -> bool | None:
        """Whether a rule naming `names` covers `name`; None when `name` is a variable."""
        if not names:
            return True
        if verb == "create" or name is None:
            return False
        if name.startswith("$") or "${" in name:
            return None
        return name in names

    def covers(grant: tuple, need: tuple) -> bool | None:
        scope, groups, resources, verbs, names = grant
        namespace, group, resource, verb, name = need
        in_scope = scope is None or (
            namespace not in (None, ALL_NAMESPACES) and scope == namespace
        )
        if not (
            in_scope
            and (group in groups or "*" in groups)
            and (resource in resources or "*" in resources)
            and (verb in verbs or "*" in verbs)
        ):
            return False
        return by_name(names, verb, name)

    gaps = []
    for need in needs:
        verdicts = [covers(g, need) for g in granted]
        if True in verdicts:
            continue
        if None in verdicts:
            raise KeyError(
                f"{need}: its name is a variable, so a resourceNames rule cannot be checked"
            )
        gaps.append(need)
    return sorted(gaps, key=lambda n: tuple(str(x) for x in n))
