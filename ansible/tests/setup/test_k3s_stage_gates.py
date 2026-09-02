"""Every gate on a k3s topic import must default to running it.

`roles/setup/k3s/tasks/main.yml` imports eight topic files. Two of them are gated so a
staging cluster can decline work that only makes sense against prod — `longhorn-backup.yml`
holds the B2/R2 credentials, `health-crons.yml` pushes into prod's Kuma and Healthchecks.io.

The hazard is direction. A gate whose default is false silently stops doing prod's work,
and nothing else in the repo would notice: ansible-lint parses the file without evaluating
the conditional, `--list-tasks` lists tasks statically, and `--check` skips the apply. The
first evidence would be a backup target that quietly stopped being reconciled.

So the invariant is that prod's behaviour is the role's behaviour with nothing overridden.
A staging host turns things off in its own host_vars; the defaults never do.

Derived from main.yml rather than listing the flags here, so a gate added later is covered
without anyone remembering to extend this file.
"""

import re

import pytest

from _helpers import ROLES, load_tasks, load_yaml

K3S = ROLES / "setup" / "k3s"
MAIN = K3S / "tasks" / "main.yml"
DEFAULTS = K3S / "defaults" / "main.yml"

# `k3s_manage_backup_targets | bool` -> k3s_manage_backup_targets. Anchored at the start so a
# compound condition yields nothing and trips the "must be a bare flag" assertion below
# rather than being silently half-checked.
BARE_FLAG = re.compile(r"^(\w+)(\s*\|\s*bool)?$")


def _gated_imports() -> list[tuple[str, str]]:
    """(imported file, raw `when:` expression) for every gated import in main.yml."""
    out = []
    for task in load_tasks(MAIN):
        target = task.get("ansible.builtin.import_tasks") or task.get("import_tasks")
        if target and "when" in task:
            when = task["when"]
            for expr in when if isinstance(when, list) else [when]:
                out.append((target, str(expr).strip()))
    return out


def test_some_imports_are_gated():
    """Guards the derivation itself: an empty list would make every test below vacuous."""
    assert _gated_imports(), (
        f"no gated imports found in {MAIN} — either the gates were removed, or the "
        f"`when:`/`import_tasks` shape this test derives from changed."
    )


@pytest.mark.parametrize("target,expr", _gated_imports())
def test_the_gate_is_a_bare_flag(target, expr):
    """A gate must be one variable, so its default is a single readable fact.

    A compound condition (`and`, a comparison, a lookup) can default to false through a term
    this file cannot see, which is exactly the direction the module docstring is about.

    DECIDED: strict, and deliberately constraining. A later gate that genuinely needs a
    second term fails here rather than being half-checked, and the fix is to widen this to
    check every term of the expression — not to exempt the gate.
    """
    assert BARE_FLAG.fullmatch(expr), (
        f"the gate on {target} in {MAIN} is {expr!r}. Keep it a bare flag optionally piped "
        f"through `| bool`, so its default can be read off {DEFAULTS.name} and enforced here."
    )


@pytest.mark.parametrize("target,expr", _gated_imports())
def test_the_gate_defaults_to_running_the_import(target, expr):
    flag = BARE_FLAG.fullmatch(expr).group(1)
    defaults = load_yaml(DEFAULTS)

    assert flag in defaults, (
        f"{target} in {MAIN} is gated on {flag}, which is not defined in {DEFAULTS}. An "
        f"undefined gate fails the play at that task rather than skipping it."
    )
    assert defaults[flag] is True, (
        f"{flag} defaults to {defaults[flag]!r} in {DEFAULTS}, so {target} does NOT run "
        f"with the role's own defaults. Prod runs the role unoverridden — a gate that "
        f"defaults off stops doing prod's work and every repo-side check stays green. "
        f"Default it true and turn it off in the staging host's host_vars."
    )


def test_the_coredns_probe_name_defaults_to_a_real_name():
    """The CoreDNS probe is gated by emptiness rather than a boolean, so check it too.

    `tasks/coredns.yml` skips its behaviour probe when `k3s_coredns_probe_name` is empty.
    That is the same hazard in a different shape: an empty default would drop the only check
    that the Corefile patch actually took effect.
    """
    defaults = load_yaml(DEFAULTS)
    name = defaults.get("k3s_coredns_probe_name")
    assert name, (
        f"k3s_coredns_probe_name is {name!r} in {DEFAULTS}. Empty skips the probe that "
        f"proves cluster DNS forwards where the Corefile says — the one check in "
        f"tasks/coredns.yml that reads behaviour rather than the ConfigMap it just wrote."
    )
