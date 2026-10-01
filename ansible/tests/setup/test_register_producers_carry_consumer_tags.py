"""A setup role's `register:` producer carries every tag its consumers carry (#3120).

WHY THIS IS A CHECK AND NOT PROSE. `register:` writes a host variable that outlives the task
that produced it, so a producer and a consumer under different tags come apart the moment a run
is scoped. `ansible/roles/setup/initial_setup/CLAUDE.md`'s fact-dependency rule has said so
since the granular tags were added — a task whose `register:` feeds other blocks carries ALL
its consumers' tags. It held because the only thing passing a narrow `--tags` value was a human
who had read that file.

The deployer now derives those tags and applies them unattended
(`scripts/deploy_tools/narrow_setup.py`, reached from `deploy_narrow.plan`). A violation is no
longer an operator's mistake; it is a GitOps tick that fails on an undefined variable, holds
the SHA and parks every other session's landing. So the invariant is asserted here.

WHAT MAKES THE INVARIANT SUFFICIENT. It closes both directions at once. A change reaching the
CONSUMER narrows to the consumer's tags, which the producer carries, so the producer runs too.
A change reaching the PRODUCER narrows to the producer's tags, a superset of every consumer's
by this rule, so every consumer runs as well.

That is why `narrow_setup` needs no `register:` edge of its own, the way it needs one for
`set_fact` (#2384). The edge was written and withdrawn in #3120: it is file-granular, so one
cross-tag read widened `initial_setup`'s answer from five tags to eight, and one of the three
added (`debloat`) is declared by a second role, which `foreign_tags` then refuses — the
narrowing stopped firing on the ranges it had fired on. This rule is the same safety per TASK.

SCOPE, and the two guards that keep it honest. Only tasks of ONE role are read: a register name
in a `templates/` file would make the rendering task a consumer, and a name produced in another
role is a pair no role's derivation sees. The two scope tests below measure both limits.

Run: uv run pytest ansible/tests/setup/test_register_producers_carry_consumer_tags.py
"""

import re
from pathlib import Path

import pytest
import yaml
from lib import yaml_fast

from _helpers import REPO
from _role_census import role_dirs

SETUP_ROLES = REPO / "ansible/roles/setup"

# The names Ansible loads from a role's `tasks/`, matching
# `scripts/deploy_tools/narrow_setup_index.py:_LOADED_EXTENSIONS`. A file this skips is one
# Ansible never runs, so it holds no producer and no consumer.
_LOADED_EXTENSIONS = (".yml", ".yaml", ".json", "")

# Ansible's unconditional tag. A producer carrying it runs under every `--tags` value, so it
# satisfies every consumer whatever the consumer carries.
_ALWAYS = "always"

# The block sections a task can nest, which `_walk` descends into.
_NESTING = ("block", "rescue", "always")

# The keys of a task that are not a read of a variable: its own `register:`, and the nested
# sections whose text belongs to the child tasks rather than to the wrapper.
_NOT_A_READ = frozenset(("register", *_NESTING))


class Task:
    """One task of a role, with the tags that actually select it.

    Attributes:
        rel: the role-relative path of the file it sits in, for the failure message.
        name: its `name:`, or `<unnamed>`.
        tags: its effective tags — its own plus every enclosing block's.
        registers: the variable it produces, as a set so the empty case needs no branch.
        reads: every scalar in its body, with its own `register:` value dropped.
    """

    def __init__(self, rel: str, task: dict, tags: frozenset[str]) -> None:
        self.rel = rel
        self.name = str(task.get("name", "<unnamed>"))
        self.tags = tags
        produced = task.get("register")
        self.registers = (
            frozenset({produced})
            if isinstance(produced, str) and produced
            else frozenset()
        )
        # Its own `register:` value is not a read of itself, and the nested sections belong to
        # the child tasks the walk visits separately — counting them here would report a block
        # wrapper as a second consumer of everything inside it.
        body = {key: value for key, value in task.items() if key not in _NOT_A_READ}
        self.reads = tuple(_scalars(body))

    def mentions(self, name: str) -> bool:
        """Whether this task reads the variable `name`.

        Word-bounded, so `optimize_pi_gz_integrity_state_file` is not read as a use of
        `optimize_pi_gz_integrity_state`. Both of the tree's two near-miss pairs are that
        shape, and a substring match would report each as a violation.
        """
        mention = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        return any(mention.search(text) for text in self.reads)

    def __str__(self) -> str:
        return f"{self.rel}: {self.name} {sorted(self.tags) or '(untagged)'}"


def _scalars(value):
    """Every scalar under `value`, as text, nested mapping keys included."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _scalars(key)
            yield from _scalars(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _scalars(inner)
    elif value is not None:
        yield str(value)


def _own_tags(task: dict) -> frozenset[str]:
    tags = task.get("tags") or []
    tags = [tags] if isinstance(tags, str) else tags
    return frozenset(str(tag) for tag in tags)


def _walk(rel: str, tasks, inherited: frozenset[str]) -> list[Task]:
    """Every task in a task list, blocks walked through and their tags carried down.

    A `block`/`rescue`/`always` mapping is not a task of its own: its `tags:` select the tasks
    inside it. The wrapper can still carry a `register:`, so it is yielded as well — that is
    the shape `ansible/tests/deploy/test_dual_tagged_producers.py` walks for the same reason.
    """
    out: list[Task] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        effective = inherited | _own_tags(task)
        out.append(Task(rel, task, effective))
        for key in _NESTING:
            if isinstance(task.get(key), list):
                out += _walk(rel, task[key], effective)
    return out


# The spellings of a STATIC task import, matching
# `scripts/deploy_tools/narrow_setup_index.py:_STATIC_IMPORTS`. An `include_tasks` is absent
# for the reason stated there: a dynamic include's tasks are selected by the include task, not
# by their own tags, so no tag of the included file selects them.
_STATIC_IMPORTS = (
    "import_tasks",
    "ansible.builtin.import_tasks",
    "ansible.legacy.import_tasks",
)


def _import_sites(tasks, inherited: frozenset[str]) -> list[tuple[frozenset[str], str]]:
    """(the tags at the import, the file name imported) for each static import in a list."""
    out: list[tuple[frozenset[str], str]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        effective = inherited | _own_tags(task)
        for key in _STATIC_IMPORTS:
            value = task.get(key)
            if isinstance(value, dict):
                value = value.get("file")
            if isinstance(value, str) and "{{" not in value:
                out.append((effective, value.rsplit("/", 1)[-1]))
        for key in _NESTING:
            if isinstance(task.get(key), list):
                out += _import_sites(task[key], effective)
    return out


def _inherited_tags(docs: dict[str, list]) -> dict[str, frozenset[str]]:
    """Each task file's inherited tags, walked out from every file through static imports.

    An IMPORTED file's tasks carry the tags of the import statement, so a file with no `tags:`
    of its own is still tag-selectable. `setup/k3s`'s `tasks/unit-logging.yml` is the live
    case: untagged, imported once from `server.yml` under `k3s_server` and once from
    `agent.yml` under `k3s_agent`, and it registers a drop-in both files then read. Reading it
    in isolation reported two violations where the invariant holds.

    EVERY file is a walk root, not just `tasks/main`. A role's entry point need not be its
    `main`: `k3s-bringup.yml` reaches `tasks/agent.yml` with a play-level `include_role` +
    `tasks_from:`, so a walk rooted at `main` alone never visits agent.yml's own import of
    `unit-logging.yml` and reports that import's register as uncovered. Seeding every file can
    only ADD tags, so it cannot invent a violation; what it can do is cover one in a file no
    playbook reaches at all, which is a file whose tasks never run.

    Import sites of the same file UNION. Any tag in the union selects one of them, so the
    producer runs for every consumer under any of those tags. A `when:` on the import
    statement is not modelled — this is a tag check, and a gate that can skip an import is the
    same hazard a whole-role run has.
    """
    out = {rel: frozenset() for rel in docs}
    seen: set[tuple[str, frozenset[str]]] = set()
    todo = [(rel, frozenset[str]()) for rel in docs]
    while todo:
        rel, tags = todo.pop()
        if rel not in docs or (rel, tags) in seen:
            continue
        seen.add((rel, tags))
        out[rel] |= tags
        todo += [(name, at) for at, name in _import_sites(docs[rel], tags)]
    return out


def role_tasks(role: Path) -> list[Task]:
    """Every task of one setup role, in no particular order.

    Raises:
        AssertionError: a task file does not parse. An unreadable file would otherwise
            contribute no producer and no consumer, and the check would pass over it.
    """
    task_dir = role / "tasks"
    if not task_dir.is_dir():
        return []
    docs: dict[str, list] = {}
    for path in sorted(p for p in task_dir.iterdir() if p.is_file()):
        if path.name.startswith(".") or path.suffix not in _LOADED_EXTENSIONS:
            continue
        try:
            doc = yaml_fast.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise AssertionError(f"{path} does not parse: {exc}") from exc
        if isinstance(doc, list):
            docs[path.name] = doc
    inherited = _inherited_tags(docs)
    out: list[Task] = []
    for name, doc in docs.items():
        out += _walk(f"tasks/{name}", doc, inherited[name])
    return out


def offenders(tasks: list[Task]) -> list[str]:
    """Each consumer whose tags its producer does not carry, as one line per pair.

    An UNTAGGED consumer is not judged. No `--tags` value selects it, so it runs only on the
    whole-role run, where the producer runs too.
    """
    out: list[str] = []
    for producer in tasks:
        for name in producer.registers:
            if _ALWAYS in producer.tags:
                continue
            for consumer in tasks:
                if consumer is producer or not consumer.tags:
                    continue
                if not consumer.mentions(name):
                    continue
                missing = consumer.tags - producer.tags
                if missing:
                    out.append(
                        f"{name}: producer {producer} does not carry "
                        f"{sorted(missing)}, which its consumer {consumer} needs"
                    )
    return out


def _roles() -> list[Path]:
    """Every setup role with a `tasks/` directory, a retired role's debris skipped."""
    return [role for role in role_dirs(SETUP_ROLES) if (role / "tasks").is_dir()]


# The setup roles this check must find a producer in. A frozenset rather than a count, so a
# role that stops registering anything — or is renamed — fails by name instead of shrinking
# the census to nothing and passing.
ROLES_WITH_A_PRODUCER = frozenset(
    {"docker_install", "initial_setup", "k3s", "optimize_pi", "sops_setup"}
)


def test_the_census_covers_every_setup_role_that_registers_anything():
    """Non-vacuity: the per-role assertion below has live subjects."""
    found = {
        role.name
        for role in _roles()
        if any(task.registers for task in role_tasks(role))
    }
    assert ROLES_WITH_A_PRODUCER <= found, sorted(ROLES_WITH_A_PRODUCER - found)


@pytest.mark.parametrize("role", _roles(), ids=lambda p: p.name)
def test_every_register_producer_carries_its_consumers_tags(role: Path):
    """The real tree. Parametrized per role so a failure names the role, not the tree."""
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Granular tags (run one block without the whole role)
    found = offenders(role_tasks(role))
    assert not found, (
        f"{role.name}: a tag-scoped run selects the consumer and not the producer, so it "
        "dies on an undefined variable — and the GitOps deployer now derives those tags "
        "itself. Add the consumer's tags to the producer:\n  " + "\n  ".join(found)
    )


def test_no_setup_role_template_reads_a_registered_name():
    """The scope guard. A register read in a template is a consumer this check cannot see.

    The task that renders the template would be the real consumer, and its tags are not the
    template's. Nothing in the tree does it, so the check stays task-only; a template that
    starts doing it fails here rather than silently leaving the invariant unchecked.
    """
    found: list[str] = []
    for role in _roles():
        names = {name for task in role_tasks(role) for name in task.registers}
        if not names:
            continue
        mention = re.compile(
            "|".join(rf"(?<!\w){re.escape(name)}(?!\w)" for name in sorted(names))
        )
        templates = role / "templates"
        for path in sorted(templates.rglob("*")) if templates.is_dir() else []:
            if not path.is_file():
                continue
            hit = mention.search(path.read_text(errors="replace"))
            if hit:
                found.append(f"{path.relative_to(REPO)} reads {hit.group(0)}")
    assert not found, (
        "a registered name is read inside a template, so the consumer is whichever task "
        "renders it and this module's task-only scope no longer covers the invariant:\n  "
        + "\n  ".join(found)
    )


# ── the proof each half can go red ──────────────────────────────────────────────────────


def _written(tmp_path: Path, body: str) -> Path:
    """One task file under a role-shaped directory, which `role_tasks` walks."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "main.yml").write_text(body)
    return tmp_path


_PRODUCER = (
    "- name: Resolve the deploy user's home\n"
    "  ansible.builtin.command: getent passwd daniel\n"
    "  changed_when: false\n"
    "  register: demo_user_home\n"
)


def test_a_consumer_under_a_tag_the_producer_lacks_is_flagged(tmp_path: Path):
    """The home-dir resolver's shape with one tag dropped from the producer."""
    role = _written(
        tmp_path,
        _PRODUCER + "  tags: [tooling]\n"
        "- name: Install the git hooks\n"
        "  ansible.builtin.command: prek install\n"
        "  environment:\n"
        '    PATH: "{{ demo_user_home.stdout }}/.local/bin"\n'
        "  tags: [git-hooks]\n",
    )
    found = offenders(role_tasks(role))
    assert len(found) == 1, found
    assert "demo_user_home" in found[0] and "git-hooks" in found[0]


def test_a_consumer_under_a_tag_the_producer_carries_is_clean(tmp_path: Path):
    """The home-dir resolver as the tree actually writes it: `[tooling, git-hooks]`."""
    role = _written(
        tmp_path,
        _PRODUCER + "  tags: [tooling, git-hooks]\n"
        "- name: Install the git hooks\n"
        "  ansible.builtin.command: prek install\n"
        "  environment:\n"
        '    PATH: "{{ demo_user_home.stdout }}/.local/bin"\n'
        "  tags: [git-hooks]\n",
    )
    assert offenders(role_tasks(role)) == []


def test_an_always_tagged_producer_is_clean(tmp_path: Path):
    """`tags: [always]` runs under every `--tags` value, so it satisfies any consumer."""
    role = _written(
        tmp_path,
        _PRODUCER + "  tags: [always]\n"
        "- name: Install the git hooks\n"
        "  ansible.builtin.command: prek install\n"
        '  args: {chdir: "{{ demo_user_home.stdout }}"}\n'
        "  tags: [git-hooks]\n",
    )
    assert offenders(role_tasks(role)) == []


def test_a_consumer_inside_a_block_is_judged_on_the_blocks_tags(tmp_path: Path):
    """A block's `tags:` select its children, so the inherited tag is the one that matters."""
    role = _written(
        tmp_path,
        _PRODUCER + "  tags: [tooling]\n"
        "- name: The git-hooks block\n"
        "  tags: [git-hooks]\n"
        "  block:\n"
        "    - name: Install the git hooks\n"
        "      ansible.builtin.command: prek install\n"
        '      args: {chdir: "{{ demo_user_home.stdout }}"}\n',
    )
    found = offenders(role_tasks(role))
    assert len(found) == 1, found
    assert "git-hooks" in found[0]


def test_an_untagged_consumer_is_not_judged(tmp_path: Path):
    """No `--tags` value selects an untagged task, so it imposes nothing on the producer."""
    role = _written(
        tmp_path,
        _PRODUCER + "  tags: [tooling]\n"
        "- name: Install the git hooks\n"
        "  ansible.builtin.command: prek install\n"
        '  args: {chdir: "{{ demo_user_home.stdout }}"}\n',
    )
    assert offenders(role_tasks(role)) == []


def test_a_longer_name_is_not_read_as_a_use_of_the_shorter_one(tmp_path: Path):
    """`optimize_pi_gz_integrity_state_file`'s shape — the tree's live near miss."""
    role = _written(
        tmp_path,
        "- name: Stat the state file\n"
        "  ansible.builtin.stat:\n"
        '    path: "{{ demo_state_file }}"\n'
        "  register: demo_state\n"
        "  tags: [health]\n"
        "- name: Render the script\n"
        "  ansible.builtin.template:\n"
        "    src: script.sh.j2\n"
        '    dest: "{{ demo_state_file }}"\n'
        "  tags: [scripts]\n",
    )
    assert offenders(role_tasks(role)) == []


def _written_pair(tmp_path: Path, main: str, imported: str) -> Path:
    """A role whose `main.yml` imports `logging.yml`, the `setup/k3s` shape."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "main.yml").write_text(main)
    (tmp_path / "tasks" / "logging.yml").write_text(imported)
    return tmp_path


_IMPORTED_PRODUCER = (
    "- name: Write the log drop-in\n"
    "  ansible.builtin.copy:\n"
    "    dest: /etc/systemd/system/demo.d/log.conf\n"
    "    content: demo\n"
    "  register: demo_dropin\n"
)
_CONSUMER = (
    "- name: Restart the unit\n"
    "  ansible.builtin.systemd_service:\n"
    "    name: demo\n"
    "    state: restarted\n"
    "  when: demo_dropin is changed\n"
    "  tags: [demo_server]\n"
)


def test_a_producer_in_an_imported_file_inherits_the_import_sites_tag(tmp_path: Path):
    """`setup/k3s`'s shape: the producer is untagged and its import statement is tagged."""
    role = _written_pair(
        tmp_path,
        "- name: Keep the unit's output in a file\n"
        "  ansible.builtin.import_tasks: logging.yml\n"
        "  tags: [demo_server]\n" + _CONSUMER,
        _IMPORTED_PRODUCER,
    )
    assert offenders(role_tasks(role)) == []


def test_a_producer_imported_only_under_another_tag_is_flagged(tmp_path: Path):
    """The same shape with the one import site under a tag the consumer does not share."""
    role = _written_pair(
        tmp_path,
        "- name: Keep the unit's output in a file\n"
        "  ansible.builtin.import_tasks: logging.yml\n"
        "  tags: [demo_agent]\n" + _CONSUMER,
        _IMPORTED_PRODUCER,
    )
    found = offenders(role_tasks(role))
    assert len(found) == 1, found
    assert "demo_server" in found[0]


def test_no_setup_role_reads_a_name_another_role_registered():
    """The second scope guard: a `register:` outlives its role, and this check is per role.

    The narrowing derives one role's tags at a time, so nothing above judges a cross-role
    pair. Nothing in the tree makes one; a first one fails here.
    """
    produced = {
        name: role.name
        for role in _roles()
        for task in role_tasks(role)
        for name in task.registers
    }
    assert produced, "no setup role registers anything — the census went empty"
    found = []
    for role in _roles():
        for task in role_tasks(role):
            for name in task.reads:
                owner = produced.get(name)
                if owner is not None and owner != role.name:
                    found.append(f"{role.name}:{task.rel} reads {name} from {owner}")
    assert not found, "\n".join(found)
