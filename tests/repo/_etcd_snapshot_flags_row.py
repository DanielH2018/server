"""The census row holding every `k3s etcd-snapshot` call site to the drop-in's long flag names."""

import re
from functools import cache


from lib.repo_paths import K3S_SERVER
from _row_table import Census, Subject, tracked
from lib import yaml_fast

_COMPONENT = K3S_SERVER / "component.yml"

# The command and its arguments, across a `\` continuation or a prose wrap onto an indented line.
# A closing backtick, a pipe or a closing parenthesis ends the call.
_CALL = re.compile(r"k3s\s+etcd-snapshot\b((?:(?:[ \t]*\\?\n[ \t]+|[ \t]+)[^\s`|)]+)*)")
_ARRAY_USE = re.compile(r"\$\{(\w+)\[@\]\}")
_FLAG = re.compile(r"(?<![\w-])--[a-z0-9-]+")


@cache
def _short_forms_of_the_drop_in() -> frozenset[str]:
    """The short spelling of each snapshot flag `k3s_server_config` sets."""
    config = yaml_fast.safe_load(_COMPONENT.read_text())["defaults"][
        "k3s_server_config"
    ]
    return frozenset(
        "--" + key.removeprefix("etcd-")
        for key in config
        if key.startswith(("etcd-s3", "etcd-snapshot-"))
    )


def _is_short(flag: str) -> bool:
    # `--s3-*` and `--dir` conflict only once the drop-in sets their key. Refusing them regardless
    # means adding such a key cannot break a call site.
    return (
        flag in _short_forms_of_the_drop_in()
        or flag.startswith("--s3-")
        or flag == "--dir"
    )


def _call_count(subject: Subject) -> int:
    return sum(1 for _ in _CALL.finditer(subject.text))


def _short_flag_offence(subject: Subject) -> list[str]:
    offences = []
    for call in _CALL.finditer(subject.text):
        arguments = call[1]
        for name in _ARRAY_USE.findall(arguments):
            array = re.search(rf"\b{name}=\(([^)]*)\)", subject.text)
            arguments += array[1] if array else ""
        line = subject.text.count("\n", 0, call.start()) + 1
        offences += [
            f"line {line} passes {flag}"
            for flag in _FLAG.findall(arguments)
            if _is_short(flag)
        ]
    return offences


def _call_site_files() -> list[str]:
    return [
        rel
        for rel in tracked(
            "docs/*.md",
            "scripts/*.py",
            "scripts/*.sh",
            "k8s/roles/*.j2",
            "k8s/roles/*.sh",
            "k8s/roles/*.py",
            "k8s/roles/*.yml",
            "hosts/*.j2",
            "hosts/*.sh",
            "hosts/*.yml",
            ".claude/*.md",
        )
        if "/tests/" not in rel and not rel.startswith("docs/archive/")
    ]


ETCD_SNAPSHOT_LONG_FLAGS = Census(
    name="etcd-snapshot-calls-use-the-drop-ins-long-flags",
    reason=(
        "`k3s etcd-snapshot` reads /etc/rancher/k3s/config.yaml.d/50-homelab.yaml, and it refuses "
        "a short form of a key that file sets: `Cannot use two forms of the same flag: etcd-s3 "
        "s3`. H2.8a's keys failed the off-box cron that way, and the same flag in a runbook fails "
        "during a disaster recovery. Use the `--etcd-*` spelling at the call site."
    ),
    files=_call_site_files,
    offence=_short_flag_offence,
    count=_call_count,
    red=(
        Subject("a.md", "`k3s etcd-snapshot list --s3` as root\n"),
        Subject("a.md", "`k3s etcd-snapshot list\n   --s3` as root\n"),
        Subject(
            "a.sh", "OUT=$(k3s etcd-snapshot save \\\n  --snapshot-compress 2>&1)\n"
        ),
        Subject("a.sh", "k3s etcd-snapshot save --snapshot-retention 28\n"),
        Subject("a.sh", 'k3s etcd-snapshot ls --s3-bucket "$B"\n'),
        Subject("a.sh", 'A=(--s3 --s3-folder x)\nk3s etcd-snapshot list "${A[@]}"\n'),
    ),
    green=(
        Subject(
            "a.md", "`k3s etcd-snapshot list --etcd-s3` as root, then --s3 elsewhere\n"
        ),
        Subject(
            "a.sh",
            "k3s etcd-snapshot save --name offbox \\\n"
            "  --etcd-snapshot-compress --etcd-s3 --etcd-s3-bucket b\n",
        ),
        Subject(
            "a.sh",
            'A=(--etcd-s3 --etcd-s3-folder x)\nk3s etcd-snapshot list "${A[@]}"\n',
        ),
        Subject("a.md", "The `k3s server --s3` form is not a snapshot call.\n"),
    ),
    must_find=frozenset(
        {
            "docs/k3s-etcd-restore.md",
            "scripts/backup/etcd_restore_drill.sh",
            "scripts/deploy_tools/runbook_gates_lib/etcd_restore.py",
        }
    ),
)
