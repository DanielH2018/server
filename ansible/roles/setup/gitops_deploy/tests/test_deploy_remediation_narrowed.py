"""What the remediation prints once the deployer has narrowed a setup role's tag (#2307).

Split out of test_deploy_remediation.py, which is at its module-length cap. The narrowed
`--tags` replaces the whole-role tag; the whole-role warning goes with it, EXCEPT where the
narrowed tags still reach the role's gated control-plane tasks, which keep a warning of their
own.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_narrowed.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_narrowed.py

import pathlib

import yaml

import deploy_remediation
import gitops_markers
import narrow_setup_index

from deploy_remediation import broad_remediation, manual_plane_remediation

_K3S_ROLE = pathlib.Path(__file__).parents[2] / "k3s"
_K3S_TASKS = _K3S_ROLE / "tasks"


def _tagged_tasks(tasks, inherited=frozenset()):
    """Every task in a parsed task list, with the tags that select it, blocks walked through.

    A block is not a task: its `tags:` apply to the tasks inside it, so the walk carries them
    down rather than yielding the block itself.
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        own = task.get("tags") or []
        own = [own] if isinstance(own, str) else own
        effective = inherited | {str(t) for t in own}
        nested = [task.get(k) for k in ("block", "rescue", "always") if task.get(k)]
        if nested:
            for inner in nested:
                yield from _tagged_tasks(inner, effective)
        else:
            yield task, effective


def _restart_handlers(path=_K3S_ROLE / "handlers" / "main.yml") -> set[str]:
    """The role's handler names that restart a service, read from `handlers/main.yml`."""
    handlers = yaml.safe_load(path.read_text())
    return {h["name"] for h in handlers if "restarted" in yaml.safe_dump(h)}


def _reachable_task_files() -> list[pathlib.Path]:
    """The task files `tasks/main.yml` statically imports, itself included, transitively.

    The scope the warning has to cover, and no wider. `tasks/agent.yml` notifies a restart of
    its own and carries `k3s_agent`, but no `--tags` this derivation prints can ever select it
    — `main.yml` does not import it, and `narrow_setup_index.RoleIndex.reachable` refuses a tag
    read off such a file. `_static_imports` is shared with that module so the two walks agree.
    """
    seen: list[pathlib.Path] = []
    todo = [_K3S_TASKS / "main.yml"]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.append(path)
        doc = yaml.safe_load(path.read_text())
        for name in narrow_setup_index._static_imports(
            doc if isinstance(doc, list) else []
        ):
            todo.append(_K3S_TASKS / name)
    return sorted(seen)


def _gated_tasks(paths=None, handlers=None) -> list[tuple[str, frozenset[str]]]:
    """Every reachable task in the role that a narrowed `--tags` must warn about, with its tags.

    `paths` and `handlers` default to the role's reachable task files and `handlers/main.yml`;
    a test passes its own to prove the notify arm fires.

    Two ways to qualify. A task naming a control-plane gate, `rotate-keys` or the log drop-in
    restart — the markers the warning's own prose names. And a task that NOTIFIES a restart
    handler, whichever file it sits in: that is the half a server.yml-only walk could not see,
    so a `notify: Restart k3s` added to `node.yml` would have let a narrowed `node-sysctl`
    take the control plane down with no warning (#2350).
    """
    markers = (
        *deploy_remediation._K3S_CONTROL_PLANE_GATES,
        "rotate-keys",
        "Restart k3s",
    )
    restarts = _restart_handlers(*([handlers] if handlers else []))
    out = []
    for path in _reachable_task_files() if paths is None else paths:
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, list):
            continue
        for task, tags in _tagged_tasks(doc):
            notify = task.get("notify") or []
            notify = [notify] if isinstance(notify, str) else notify
            dumped = yaml.safe_dump(task)
            if any(m in dumped for m in markers) or (set(notify) & restarts):
                out.append((f"{path.name}:{task.get('name')}", frozenset(tags)))
    return out


# ── #2307: a derived narrow tag replaces the role tag, and the warning with it ──────────────


def test_a_narrowed_role_prints_its_own_tags_and_drops_the_warning():
    """The narrowing's whole point, on both composers.

    The warning describes what `--tags k3s` does. Printed beside `--tags kubeconfig` it would
    warn about a run the operator is not being told to make, which is how a real warning gets
    read as boilerplate.
    """
    narrow = {"k3s": frozenset({"kubeconfig"})}
    for cmd in (
        broad_remediation(False, True, {"k3s"}, narrow_tags=narrow),
        manual_plane_remediation({"k3s"}, narrow),
    ):
        assert "ansible/k3s-bringup.yml --tags kubeconfig" in cmd
        assert "WARNING" not in cmd


def test_a_role_the_derivation_refused_keeps_the_role_tag_and_the_warning():
    """The rejecting half: an empty tag set is a refusal, not an empty `--tags` value.

    `--tags` with nothing after it runs the WHOLE playbook, so a refusal that leaked through
    as an empty string would prescribe every setup role on the host.
    """
    for narrow in ({}, {"k3s": frozenset()}):
        cmd = manual_plane_remediation({"k3s"}, narrow)
        assert "ansible/k3s-bringup.yml --tags k3s" in cmd
        assert "WARNING" in cmd


def test_several_narrow_tags_are_one_comma_joined_tags_value():
    """Two ranges can make one role pending, and both tags have to run."""
    narrow = {"k3s": frozenset({"kubeconfig", "coredns"})}
    assert "--tags coredns,kubeconfig" in manual_plane_remediation({"k3s"}, narrow)


# ── a narrowed tag that still reaches the gated tasks keeps the warning (#2324 review) ──


def test_a_narrowed_tag_reaching_the_gated_tasks_keeps_the_warning():
    """`--tags k3s_server` arms the restart and the re-encryption as surely as `--tags k3s`.

    Every task in `tasks/server.yml` carries `k3s_server`, so a range touching that file
    narrows to it. Dropping the warning there would print the dangerous command bare.
    """
    for narrow in (
        {"k3s": frozenset({"k3s_server"})},
        {"k3s": frozenset({"k3s_server", "kubeconfig"})},
    ):
        cmd = manual_plane_remediation({"k3s"}, narrow)
        assert "--tags " in cmd and "k3s_server" in cmd.split("WARNING")[0]
        assert "WARNING" in cmd
        assert "rotate-keys" in cmd, "the warning still names the re-encryption"
        assert "WHOLE role" not in cmd, "the narrowed command is not the whole role"


def test_the_gated_tags_are_every_tag_the_gated_tasks_carry():
    """Derived from the whole ROLE, so a gated task retagged or moved cannot slip the warning.

    The deployer's venv cannot import yaml, so `MAXIMAL_ROLE_GATED_TAGS` is a constant there;
    this is where it is checked against the role. The walk covers every task file rather than
    `server.yml` alone: nothing else notifies `Restart k3s` today, and the day one does, a
    narrowed `--tags` naming its tag would take the control plane down silently (#2350).
    """
    reachable = {p.name for p in _reachable_task_files()}
    assert {"main.yml", "server.yml", "node.yml"} <= reachable, sorted(reachable)
    assert "agent.yml" not in reachable, "agent.yml is not reached by the role's entry"
    gated = _gated_tasks()
    assert len(gated) >= 3, f"only {len(gated)} gated tasks found in the role"
    assert any(name.startswith("server.yml:") for name, _ in gated), (
        "the walk found no gated task in tasks/server.yml, so it is scanning nothing"
    )
    carried = set().union(*(tags for _, tags in gated))
    assert "k3s_server" in carried, sorted(carried)
    watched = deploy_remediation.MAXIMAL_ROLE_GATED_TAGS["k3s"]
    unwatched = {name: sorted(tags - watched) for name, tags in gated if tags - watched}
    assert not unwatched, (
        f"gated tasks carry tags the warning does not watch: {unwatched}"
    )


def test_the_banners_warning_roles_are_the_remediations_warning_roles():
    """`gitops_markers` holds the banner's one-line warnings, this module the long prose.

    Two maps keyed by role, in two files, because the SessionStart banner cannot import this
    module. A role added to one and not the other prints its control-plane apply bare on one
    surface.
    """
    short = set(gitops_markers.MAXIMAL_ROLE_WARNING)
    assert "k3s" in short, "the census is empty, so it compares nothing"
    assert short == set(deploy_remediation._MAXIMAL_ROLE_TAGS)
    assert set(deploy_remediation.MAXIMAL_ROLE_GATED_TAGS) == set(
        deploy_remediation._MAXIMAL_ROLE_GATED_WARNING
    )
    assert set(deploy_remediation.MAXIMAL_ROLE_GATED_TAGS) <= short


def test_a_task_notifying_a_restart_outside_server_yml_would_be_found(tmp_path):
    """The rejecting half: the notify arm fires on a task the marker arm does not match.

    Driven through `_gated_tasks` itself, so deleting its notify arm fails here. The role's
    one restart handler today is `Restart k3s`, which the marker arm matches by name, so the
    handler here is renamed: a second restart handler is exactly what the arm exists for.
    The quiet twin notifying nothing is the control, and must not count.
    """
    handlers = tmp_path / "handlers.yml"
    handlers.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Bounce the API server",
                    "ansible.builtin.systemd": {"state": "restarted"},
                }
            ]
        )
    )
    sysctl = {"ansible.builtin.sysctl": {"name": "fs.inotify.max_user_instances"}}
    node = tmp_path / "node.yml"
    node.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Raise and bounce",
                    **sysctl,
                    "notify": "Bounce the API server",
                    "tags": ["node-sysctl"],
                },
                {"name": "Raise quietly", **sysctl, "tags": ["node-quiet"]},
            ]
        )
    )
    assert _gated_tasks([node], handlers) == [
        ("node.yml:Raise and bounce", frozenset({"node-sysctl"}))
    ]


# ── #2349: the clear command names the tags the apply beside it ran ─────────────────────


def test_a_narrowed_apply_prints_a_clear_that_names_what_it_applied():
    """A bare clear drops the whole line, and the row can grow between print and run.

    A second PR touching the same role widens the row to `coredns,kubeconfig` before the
    operator clears. The bare command would take `coredns` with it, leaving that change
    merged, unapplied and recorded nowhere.
    """
    cmd = manual_plane_remediation({"k3s"}, {"k3s": frozenset({"kubeconfig"})})
    assert "clear-manual-plane k3s --applied kubeconfig" in cmd


def test_a_whole_role_apply_prints_the_bare_clear():
    """The rejecting half: a whole-role apply covers whatever the row gained, so it takes it.

    `--applied` there would be wrong in the other direction — it would leave a line pending
    for a tag the operator just ran.
    """
    cmd = manual_plane_remediation({"k3s"}, {})
    assert "clear-manual-plane k3s`" in cmd
    assert "--applied" not in cmd


def test_two_roles_print_one_clear_each_and_only_the_narrowed_one_says_applied():
    """`common`'s row is always empty, so `--applied` there would keep its line for good.

    A shared `<role> --applied <tags>` placeholder sent the operator to do exactly that, and
    its `<` read as a shell redirect when pasted.
    """
    cmd = manual_plane_remediation(
        {"k3s", "common"}, {"k3s": frozenset({"kubeconfig"})}
    )
    assert "clear-manual-plane common && " in cmd
    assert "clear-manual-plane k3s --applied kubeconfig`" in cmd
    assert "<" not in cmd.rsplit(", then ", 1)[-1], "the clear names no placeholder"
