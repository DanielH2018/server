"""config_files leaves a chezmoi-managed host's dotfiles alone (#2322).

chezmoi owns `.bashrc` and the templated `.gitconfig`, which carries commit signing, on
daniel-box and daniel-server. `config_files` used to copy its own versions over them on every
host. Each copy is now gated on a `stat` of the chezmoi source directory, and the stat carries
both sub-tags so a `--tags bash` or `--tags git` run still sets the register the gate reads.

Run: uv run pytest ansible/tests/setup/test_config_files_skips_chezmoi_hosts.py
"""

from lib import yaml_fast

from _helpers import ROLES, walk_tasks

TASKS = ROLES / "setup" / "config_files" / "tasks" / "main.yml"
GUARD = "config_files_chezmoi_source.stat.exists"


def _home_copies(tasks):
    return [t for t in walk_tasks(tasks) if "ansible.builtin.copy" in t]


def _unguarded(tasks):
    """Names of home-directory copies whose `when:` does not read the chezmoi stat."""
    return sorted(
        t["name"] for t in _home_copies(tasks) if GUARD not in str(t.get("when", ""))
    )


def _tags(task):
    tags = task.get("tags", [])
    return {tags} if isinstance(tags, str) else set(tags)


def test_every_dotfile_copy_is_gated_on_the_chezmoi_stat():
    tasks = yaml_fast.safe_load(TASKS.read_text())
    copies = _home_copies(tasks)
    # Non-vacuity: the two files this role ships, by name.
    assert {"Copy .bashrc to home directory", "Copy .gitconfig to home directory"} <= {
        t["name"] for t in copies
    }
    assert _unguarded(tasks) == []


def test_an_unguarded_copy_is_flagged():
    tasks = [
        {
            "name": "Copy .bashrc to home directory",
            "ansible.builtin.copy": {},
            "tags": "bash",
        }
    ]
    assert _unguarded(tasks) == ["Copy .bashrc to home directory"]


def test_the_stat_runs_under_every_sub_tag_a_copy_carries():
    tasks = yaml_fast.safe_load(TASKS.read_text())
    stat = next(
        t
        for t in walk_tasks(tasks)
        if t.get("register") == "config_files_chezmoi_source"
    )
    copy_tags = set().union(*(_tags(t) for t in _home_copies(tasks)))
    assert copy_tags == {"bash", "git"}
    assert copy_tags <= _tags(stat)
