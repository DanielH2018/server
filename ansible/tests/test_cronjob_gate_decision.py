"""What `k8s/cronjob-gate` does when its one-off run fails.

The role's contract is narrower than "the deploy succeeded": it proves the new image RUNS, not
that the workload did its job. That split is a decision, not an implementation detail —
`configarr/tasks/main.yml` records the incident behind it, where a wrapper was retired for
failing deploys over transient *arr outages — and it only holds because auto-deploy fires on an
`_image:`-only diff, so the single thing that changed is the image:

  * the container never reached its entrypoint  -> the new image's fault -> FAIL the deploy;
  * the container ran and exited non-zero       -> the application's     -> report, continue.

Everything else — a state this role has never heard of, or no state readable at all — is fatal.
That direction is deliberate and is what these tests exist to pin: the classification is an
ALLOWLIST over `cronjob_gate_ran_reasons`, so a reason string Kubernetes adds later cannot
quietly land on the non-fatal side. Inverting it to a denylist over
`cronjob_gate_start_failure_reasons` would look equivalent and would silently pass every future
unknown state.

**These tests exercise the decision, not the deploy.** They render the role's real `set_fact`
expressions against synthetic container-state payloads. `kubectl` in this repo authenticates as
a read-only ServiceAccount and Ansible is the only write path to the cluster, so a genuine
ImagePullBackOff or a genuine non-zero exit cannot be constructed here — the live path from a
broken image through to a failed play is **unexercised**, and nothing below should be read as
covering it.

**Read this before adding a case.** The first version of this module injected synthetic
`stdout_lines` straight into the `set_fact` — and the command that produces `stdout_lines` was
broken. `ansible.builtin.command` shlex-splits a `cmd:` string, the space inside
`{range .items[*]}` tore the jsonpath in two, kubectl returned rc=1, and `failed_when: false`
turned that into an empty read on every deploy. Empty classifies as fatal, so the entire
non-fatal branch was unreachable and the shipped behaviour was the blanket fail this role was
narrowed to avoid. Every test here passed, and four mutation tests passed, because all of them
operated downstream of the break.

That is the `argparse-only test hid a dead path` shape, occurring inside a module whose own
docstring already named that hazard. Naming a hazard is not covering it. The transport tests
below cover it — they assert the argv token list and run the jsonpath against the live API —
and the rule that follows from it is: **a synthetic payload must enter at the same seam the
real one does, or the test proves nothing about the path in between.**

Three further limits, stated rather than papered over:

  * `jinja2.nativetypes` renders the expressions here; Ansible renders the same source to the
    strings "True"/"False", which the tasks' `when: cronjob_gate_fatal | bool` coerces
    identically. `test_the_decision_is_wired_to_the_two_outcome_tasks` is what keeps that
    coupling honest — without it these would test an expression nothing consumes, the shape
    that once left two `probe.py` commands broken behind passing argparse tests;
  * the expressions are read out of the live role by task name. A rename fails the extraction
    loudly rather than skipping the assertions;
  * the live check proves kubectl PARSES the jsonpath, not that a broken image produces the
    reasons this module assumes. That last hop is still unexercised.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

_ROLE = Path(__file__).resolve().parents[2] / "ansible/roles/k8s/cronjob-gate"
_READ = "Read the container states of the failed gate run"
_COLLECT = "Collect the container states read"
_CLASSIFY = "Classify the gate failure"
_REPORT = "Report a non-fatal gate failure"
_FAIL = "Fail on a gate run whose container never started"


def _tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks/main.yml").read_text()) or []


def _task(fragment: str) -> dict:
    for task in _tasks():
        if fragment in str(task.get("name", "")):
            return task
    raise AssertionError(
        f"no task in cronjob-gate whose name contains {fragment!r} — the role was renamed or "
        "restructured, and these tests would otherwise assert nothing"
    )


def _defaults() -> dict:
    return yaml.safe_load((_ROLE / "defaults/main.yml").read_text()) or {}


def _classify(stdout_lines: list[str]) -> dict:
    """Render the role's own facts against a synthetic container-state read.

    Both set_fact tasks, in file order, threading `cronjob_gate_reasons` from the first into the
    second — because that is how Ansible runs them. Rendering only the second and injecting a
    hand-built `cronjob_gate_reasons` would skip the PodInitializing filter, which is exactly
    the "inject past the step you are supposed to be testing" mistake this module already made
    once, one layer further out.
    """
    env = NativeEnvironment()
    context = {
        "cronjob_gate_states": {"stdout_lines": stdout_lines},
        **{
            key: _defaults()[key]
            for key in (
                "cronjob_gate_ran_reasons",
                "cronjob_gate_start_failure_reasons",
            )
        },
    }
    rendered: dict = {}
    for fragment in (_COLLECT, _CLASSIFY):
        for name, expr in _task(fragment)["ansible.builtin.set_fact"].items():
            rendered[name] = env.from_string(str(expr)).render(context)
            context[name] = rendered[name]
    return rendered


# (payload, fatal, start_failures) — the payload is what the role's jsonpath emits: one line per
# container per state field, blank where that field is unset.
_CASES = [
    pytest.param(["ImagePullBackOff", ""], True, ["ImagePullBackOff"], id="image-pull"),
    pytest.param(["", "StartError"], True, ["StartError"], id="exec-format"),
    pytest.param(
        ["CreateContainerConfigError", ""],
        True,
        ["CreateContainerConfigError"],
        id="bad-config",
    ),
    pytest.param(["", "Error"], False, [], id="ran-and-exited-nonzero"),
    pytest.param(
        ["", "Completed", "", "Error"], False, [], id="init-ok-then-app-failure"
    ),
    pytest.param(
        ["", "Completed", "CreateContainerError", ""],
        True,
        ["CreateContainerError"],
        id="init-ok-then-start-failure",
    ),
    pytest.param(
        ["", "Error", "PodInitializing", ""], False, [], id="init-failed-app-failure"
    ),
    pytest.param(
        ["ImagePullBackOff", "", "PodInitializing", ""],
        True,
        ["ImagePullBackOff"],
        id="init-could-not-pull",
    ),
    pytest.param(
        ["", "", "PodInitializing", ""],
        True,
        [],
        id="only-podinitializing-fails-closed",
    ),
    pytest.param([], True, [], id="no-pods-at-all"),
    pytest.param(["", "", ""], True, [], id="every-state-field-blank"),
    pytest.param(["", "OOMKilled"], True, [], id="unknown-reason-fails-closed"),
]


@pytest.mark.parametrize(("stdout_lines", "fatal", "start_failures"), _CASES)
def test_container_state_decides_whether_the_gate_fails_the_deploy(
    stdout_lines: list[str], fatal: bool, start_failures: list[str]
) -> None:
    """Each synthetic container state must land on the intended side of the split.

    `unknown-reason-fails-closed` is the case that distinguishes the allowlist from a
    denylist: OOMKilled is in neither list, and it must be fatal. Under a denylist over
    `cronjob_gate_start_failure_reasons` it would pass silently, and so would every reason
    string added to Kubernetes after this was written.

    `no-pods-at-all` is not hypothetical either — a Job that hits its `activeDeadlineSeconds`
    has its pods deleted by the Job controller, so a hung run reaches exactly this payload.

    The three `PodInitializing` cases cover the direction the allowlist reasoning originally
    missed. An init container that FAILS leaves the main container at
    `waiting.reason: PodInitializing`, which is in neither list — so left in the stream it would
    make an application failure fatal, the exact case this role exists not to fail on. The role
    drops it before classifying, which leaves the decision resting on the init container's own
    reason: `Error` there is non-fatal, `ImagePullBackOff` there is fatal, and PodInitializing
    with nothing beside it is fatal because no outcome was readable at all.
    """
    result = _classify(stdout_lines)
    assert bool(result["cronjob_gate_fatal"]) is fatal
    assert list(result["cronjob_gate_start_failures"]) == start_failures


def test_start_failure_reasons_only_choose_the_message_never_the_outcome() -> None:
    """Dropping every entry from the start-failure list must not make anything non-fatal.

    The list names reasons for the operator's benefit — "the new image could not start" rather
    than "unrecognised state" — and it must not be load-bearing for the decision. A list that
    looked like it narrowed the fatal set would read as a guarantee it cannot give: this repo
    has already been bitten by a rule that appeared to narrow a verb and did not.
    """
    facts = _task(_CLASSIFY)["ansible.builtin.set_fact"]
    env = NativeEnvironment()
    for payload in (["ImagePullBackOff", ""], ["", "StartError"], ["", "OOMKilled"]):
        rendered = env.from_string(str(facts["cronjob_gate_fatal"])).render(
            cronjob_gate_states={"stdout_lines": payload},
            cronjob_gate_ran_reasons=_defaults()["cronjob_gate_ran_reasons"],
            cronjob_gate_start_failure_reasons=[],
        )
        assert bool(rendered), (
            f"{payload} stopped being fatal once the start-failure list was emptied, so that "
            "list is deciding the outcome rather than the wording"
        )


def test_ran_reasons_is_the_allowlist_and_holds_both_members() -> None:
    """`Completed` and `Error` are both required, and for different reasons.

    `Error` is the application-failure case the whole split exists for. `Completed` is the
    quieter one: an init container that succeeded reports it even when a later container
    failed, so dropping it would make every init-container caller's application failure read
    as fatal — a blanket fail reintroduced by omission rather than by edit.
    """
    assert set(_defaults()["cronjob_gate_ran_reasons"]) == {"Completed", "Error"}


def test_the_decision_is_wired_to_the_two_outcome_tasks() -> None:
    """`cronjob_gate_fatal` must actually gate the fail task and the warning, in that polarity.

    Without this the expressions above could be perfectly correct and consumed by nothing —
    the failure shape that left two `probe.py` commands broken since the k3s cutover behind
    tests that only ever exercised argparse.
    """
    fail_when = " ".join(str(c) for c in _task(_FAIL)["when"])
    report_when = " ".join(str(c) for c in _task(_REPORT)["when"])

    assert "cronjob_gate_fatal | bool" in fail_when
    assert "not (cronjob_gate_fatal | bool)" not in fail_when, (
        "the fail task is gated on NOT fatal — the two outcomes are inverted"
    )
    assert "not (cronjob_gate_fatal | bool)" in report_when, (
        "the warning is not gated on the non-fatal branch, so it would fire on every failure"
    )
    assert "ansible.builtin.fail" in _task(_FAIL)
    assert "ansible.builtin.debug" in _task(_REPORT), (
        "the non-fatal outcome must report rather than fail; a second fail task would make the "
        "split decorative"
    )


def test_every_outcome_task_stays_off_a_no_mutation_run() -> None:
    """All three post-poll tasks read a Job that k8s_no_mutate never created.

    Ansible short-circuits a `when` list, so the guard clause has to come FIRST — otherwise a
    dry run dereferences `cronjob_gate_result.stdout` on a skipped task and dies before the
    guard is evaluated.
    """
    for fragment in (
        "Read the container states",
        _CLASSIFY,
        "Show the failed gate log",
        _REPORT,
        _FAIL,
    ):
        clauses = [str(c) for c in _task(fragment)["when"]]
        assert clauses[0] == "not (k8s_no_mutate | bool)", (
            f"{fragment!r} does not lead with the no-mutation guard: {clauses}"
        )


# ------------------------------------------------------------------------------- the transport


def _read_module() -> dict:
    return _task(_READ)["ansible.builtin.command"]


def _jsonpath_argv() -> list[str]:
    argv = _read_module().get("argv")
    assert isinstance(argv, list), (
        "the container-state read no longer uses argv. See "
        "test_the_state_read_never_goes_back_to_a_shell_string for why that breaks it."
    )
    return [str(a) for a in argv]


def test_the_state_read_never_goes_back_to_a_shell_string() -> None:
    """The container-state read must use `argv:`, and the jsonpath must be one whole token.

    This is the regression that already happened, so it is the one worth pinning.
    `ansible.builtin.command` shlex-splits a `cmd:` string. The space inside `{range .items[*]}`
    tore the jsonpath into two arguments and the quotes around the newlines were stripped;
    kubectl answered `error: name cannot be provided when a selector is specified` with rc=1,
    `failed_when: false` swallowed it, and `stdout_lines` came back empty on every run. Empty
    reads as "no state readable", which is fatal — so the whole non-fatal branch this role
    exists to provide was unreachable code, and every application failure failed the deploy.

    It survived a full round of review and four mutation tests because those injected synthetic
    `stdout_lines` straight into the set_fact, downstream of the break. That is the
    `argparse-only test hid a dead path` shape occurring inside the module whose own docstring
    warns about it — which is the clearest available demonstration that naming a hazard is not
    the same as covering it. This test covers it: it asserts the transport, not the decision.
    """
    module = _read_module()
    assert "cmd" not in module, (
        "the container-state read is back to a `cmd:` string, which command shlex-splits — the "
        "jsonpath's spaces tear it into separate arguments and the read returns nothing"
    )
    argv = _jsonpath_argv()
    jsonpath = [a for a in argv if a.startswith("jsonpath=")]
    assert len(jsonpath) == 1, (
        f"expected exactly one whole jsonpath token in argv, got {jsonpath!r} — a jsonpath split "
        "across two argv entries fails the same way the cmd string did"
    )
    assert "{range .items[*]}" in jsonpath[0], (
        "the jsonpath token lost the space inside `{range .items[*]}`, so either it was "
        "reflowed or it is no longer the expression that was verified against the cluster"
    )
    assert '{"\\n"}' in jsonpath[0], (
        "the jsonpath's quoted newlines are gone; without them every container's reasons land "
        "on one line and stdout_lines cannot separate them"
    )


def test_the_jsonpath_could_not_have_survived_as_a_shell_string() -> None:
    """Prove the argv form is load-bearing rather than stylistic.

    Rebuilding the command as a single string and shlex-splitting it — exactly what
    `ansible.builtin.command` does with `cmd:` — must NOT reproduce the argv list. If it ever
    does, the jsonpath has lost the spaces and quoting that made the shell-string form fail, and
    the test above is guarding a hazard that no longer exists in this command.
    """
    argv = _jsonpath_argv()
    assert shlex.split(" ".join(argv)) != argv, (
        "shlex round-trips this argv unchanged, so the cmd-string form would work too and this "
        "module is pinning a transport that no longer needs pinning"
    )


def test_the_jsonpath_parses_against_the_live_api() -> None:
    """kubectl must accept the role's own jsonpath token verbatim.

    A read, which the read-only ServiceAccount permits, and the only check here that involves a
    real API server. Deliberately selects a job-name that matches nothing: an empty result still
    proves the expression PARSES, and it does not depend on any particular Job existing. Skipped
    with no cluster, so CI stays green.
    """
    kubectl = shutil.which("kubectl") or shutil.which("k3s")
    if kubectl is None:
        pytest.skip("no kubectl on PATH")
    argv = _jsonpath_argv()
    jsonpath = next(a for a in argv if a.startswith("jsonpath="))
    probe = [
        kubectl,
        *(["kubectl"] if kubectl.endswith("k3s") else []),
        "get",
        "pods",
        "--request-timeout=5s",
        "--selector=batch.kubernetes.io/job-name=cronjob-gate-jsonpath-selftest",
        "-o",
        jsonpath,
    ]
    done = subprocess.run(probe, capture_output=True, text=True, check=False)
    if "connection refused" in done.stderr or "was refused" in done.stderr:
        pytest.skip("no reachable cluster")
    assert done.returncode == 0, (
        f"kubectl rejected the role's jsonpath: {done.stderr.strip()!r}. The expression is "
        "wrong, or the argv form lost a token boundary."
    )


# --------------------------------------------------------------- the timeout ordering contract


def _gate_include(role: Path) -> dict | None:
    """The task in this role that includes k8s/cronjob-gate, or None.

    Matched on the include's own `name` after a yaml.safe_load, not by searching the file for
    the string. A raw substring match pulls in a role that merely MENTIONS the role in a
    comment, and then checks its unrelated `activeDeadlineSeconds` values against a timeout it
    never sets — a false failure rather than a missed one, but still a guard that fires on the
    wrong role. Parsing also makes the lookup order-independent: `vars:` above
    `ansible.builtin.include_role:` is valid YAML that Ansible runs identically.

    The cheap substring check stays as a PREFILTER so only real candidates are parsed. Every
    role's tasks file must parse for Ansible to run it, but a guard that yaml-loads all ~50 of
    them would start failing on a role it has no business reading.
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file() or "k8s/cronjob-gate" not in tasks.read_text():
        return None
    for task in yaml.safe_load(tasks.read_text()) or []:
        if (task.get("ansible.builtin.include_role") or {}).get("name") == (
            "k8s/cronjob-gate"
        ):
            return task
    return None


def _cronjob_gate_callers() -> list[Path]:
    """Roles that actually include k8s/cronjob-gate, comments excluded."""
    return [
        role
        for role in sorted(_ROLE.parent.iterdir())
        if role.is_dir() and _gate_include(role) is not None
    ]


def _effective_timeout(role: Path) -> int:
    """`cronjob_gate_timeout` as this caller will actually see it."""
    include = _gate_include(role)
    override = ((include or {}).get("vars") or {}).get("cronjob_gate_timeout")
    if override is not None:
        return int(override)
    return int(_defaults()["cronjob_gate_timeout"])


_DEADLINE = re.compile(r"^\s*activeDeadlineSeconds:\s*(.+?)\s*$", re.MULTILINE)
_JINJA_VAR = re.compile(r"^\{\{\s*([\w.]+)\s*\}\}$")


def _active_deadlines(role: Path) -> list[tuple[str, int]]:
    """Every `activeDeadlineSeconds` the role renders, as (template, seconds).

    Resolves a single-variable Jinja value against the role's own defaults — configarr writes
    `activeDeadlineSeconds: {{ configarr_k8s_timeout }}`. Anything else raises rather than being
    skipped: a deadline this cannot read is a deadline the rule below silently stops covering,
    and that is how the rule got broken in the first place.
    """
    out: list[tuple[str, int]] = []
    role_defaults = yaml.safe_load((role / "defaults/main.yml").read_text()) or {}
    tdir = role / "templates"
    for template in sorted(tdir.glob("*.j2")) if tdir.is_dir() else []:
        for raw in _DEADLINE.findall(template.read_text()):
            if raw.isdigit():
                out.append((template.name, int(raw)))
                continue
            var = _JINJA_VAR.match(raw)
            resolved = role_defaults.get(var.group(1)) if var else None
            if resolved is None:
                raise AssertionError(
                    f"{role.name}/{template.name}: activeDeadlineSeconds is {raw!r}, which this "
                    "guard cannot resolve to a number. Make it a literal or a plain variable "
                    "from the role's defaults — an unreadable deadline is one the timeout rule "
                    "stops covering, silently."
                )
            out.append((template.name, int(resolved)))
    return out


def test_gate_timeout_exceeds_every_callers_active_deadline() -> None:
    """The gate must outlast the Job's own deadline, for every caller.

    Set the other way round, the poll's retries run out first and Ansible aborts the play with a
    bare "ran out of retries" — no role-authored message, no container states, no log. Set this
    way, the Job reaches a terminal `Failed` condition first, the poll returns, and the operator
    gets the role's own failure text saying what it could and could not read.

    Executable rather than prose because the prose version was already violated by the shipped
    default on the day it was written: `cronjob_gate_timeout: 300` against pi-peer-backup's
    `activeDeadlineSeconds: 600`. This repo's escalation ladder says a rule a machine enforces
    beats a paragraph an agent has to remember, and this was the third prose-only rule in one
    slice found already broken.

    Scoped to roles that actually include the gate, so it starts covering a caller on the commit
    that wires it rather than needing to be remembered then.
    """
    callers = _cronjob_gate_callers()
    assert callers, (
        "no role includes k8s/cronjob-gate, so this guard is checking nothing — either the "
        "callers were removed or the include was renamed"
    )
    offenders = []
    for role in callers:
        timeout = _effective_timeout(role)
        for template, deadline in _active_deadlines(role):
            if timeout <= deadline:
                offenders.append(
                    f"{role.name}: cronjob_gate_timeout {timeout}s does not exceed "
                    f"{template}'s activeDeadlineSeconds {deadline}s"
                )
    assert not offenders, (
        "Caller(s) whose gate gives up before their own Job's deadline can fire, so the failure "
        "arrives as retry exhaustion with no message and no log. Raise cronjob_gate_timeout "
        "(the default, or a per-caller override in the include's vars):\n  "
        + "\n  ".join(offenders)
    )


def test_a_comment_mentioning_the_gate_does_not_make_a_role_a_caller(
    tmp_path: Path,
) -> None:
    """Only a real include counts, so the timeout guard fires on the right roles.

    A raw substring search over tasks/main.yml pulled in any role that merely NAMED
    k8s/cronjob-gate — a comment, a TODO, a note about why it is not used — and then checked
    that role's unrelated activeDeadlineSeconds against a timeout it never sets. Over-inclusive
    rather than unsafe, but a guard that fails on a role it has no business reading is a guard
    people learn to skip.
    """
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "# widget's CronJob could use k8s/cronjob-gate once it is idempotent\n"
        "- name: Deploy widget\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
    )
    assert _gate_include(role) is None

    (role / "tasks" / "main.yml").write_text(
        "- name: Gate the widget deploy on a one-off run\n"
        "  vars:\n"
        "    cronjob_gate_name: widget\n"
        "    cronjob_gate_timeout: 900\n"
        "  ansible.builtin.include_role:\n"
        "    name: k8s/cronjob-gate\n"
    )
    assert _gate_include(role) is not None
    assert _effective_timeout(role) == 900


def test_no_operator_message_carries_an_embedded_newline() -> None:
    """A folded `>-` scalar must fold, and a more-indented line silently stops it folding.

    YAML preserves a more-indented line inside a folded block verbatim and keeps a newline at
    the transition back to the base indent. A Jinja ternary whose `else` sat one level in was
    enough: the failure message broke mid-sentence at "does not\\nrecognise", in the very text
    added so an operator could tell an activeDeadlineSeconds hang from a broken image.

    Cosmetic, and caught by a reviewer rather than by anything here — which is the argument for
    the assertion. Re-indenting is the fix; nothing about the message's content prevents it
    recurring the next time one is edited.
    """
    offenders = []
    for task in _tasks():
        for module in ("ansible.builtin.fail", "ansible.builtin.debug"):
            msg = (task.get(module) or {}).get("msg", "")
            if "\n" in str(msg):
                offenders.append(f"{task.get('name')}: {module}")
    assert not offenders, (
        "operator message(s) with a literal newline — a continuation line inside the folded "
        "`msg:` block is indented deeper than the block's base, so YAML stopped folding it:\n  "
        + "\n  ".join(offenders)
    )
