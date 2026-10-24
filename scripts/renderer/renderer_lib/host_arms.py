r"""The `activate:` field of a file entry and the `ufw:` key, which feed the reconcile manifest.

An `activate:` field names what reconcile does when the installed file's hash changes
(`hosts/lib/reconcile_lib/activate.py` documents each action)::

    files:
      - {src: sshd.conf, dst: /etc/ssh/sshd_config.d/50-homelab.conf, activate: "reload:ssh.service"}

An entry with no `activate:` is activated by nothing, which is `none` spelled out. The renderer
checks the grammar that reconcile's `valid_activations` reads, so a render it would refuse fails
here.

A `ufw:` key declares firewall rules for reconcile's ufw arm (`hosts/lib/reconcile_lib/ufw.py`)::

    ufw:
      rules:
        - {id: lan-ssh, spec: "allow from {{ lan_subnet }} to any port 22 proto tcp",
           keep: true, position: first, before: ssh-limit}
        - {id: ssh-limit, spec: "limit 22/tcp", keep: true}
      legacy:
        - {spec: "allow 22/tcp", comment: "", replaced_by: ssh-limit}

Each `spec` is the rule exactly as `ufw show added` prints it after `ufw `, and each value renders
with the host's context. `keep` defaults to false, `position` to `last`, `before` to none and a
legacy `comment` to the empty string. Only a host package carries rules, because only its manifest
reaches reconcile, and the same holds for the three arms below.

A `libvirt:` key declares networks and nwfilters (`hosts/lib/reconcile_lib/libvirt.py`)::

    libvirt:
      networks:  [{name: staging, src: templates/staging-net.xml.j2}]
      nwfilters: [{name: staging-egress-fence, src: templates/staging-fence.xml.j2}]

Each `src` renders to the object's XML, which must carry the declared `<name>` and a `<uuid>`: the
pinned UUID is what lets `net-define` replace the live object instead of colliding with it. The
XML installs under `/usr/lib/homelab/libvirt/<kind>/<name>.xml`.

A `users:` key declares system users with a fixed uid (`hosts/lib/reconcile_lib/users.py`)::

    users: [{name: claude, uid: 1001, home: /home/claude, shell: /bin/bash, comment: Claude agent}]

The renderer writes every user of the host package to `/usr/lib/sysusers.d/homelab.conf`.

A `lines:` key declares edits in files dpkg does not overwrite (`hosts/lib/reconcile_lib/lines.py`)::

    lines:
      - {path: /etc/sysctl.conf, line: "vm.swappiness=10", regexp: '^\s*vm\.swappiness\s*=',
         activate: sysctl}
      - {path: /etc/zsh/zshenv, block: "export X=1", marker: "# {mark} MANAGED BLOCK"}

`activate:` joins the manifest's `activations` map under the edited path, so reconcile runs the
action when the file changes. A `mode:` is the octal mode of a file the arm has to create.
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

from renderer.renderer_lib.cluster import RenderRefused

_UNIT = r"[A-Za-z0-9@_.:-]+\.(?:service|timer|socket|path)"
# The actions `hosts/lib/reconcile_lib/activate.py:ACTION` reads.
ACTION = re.compile(
    rf"none|sysctl|modules-load|reboot|(?:try-restart|reload|manual):{_UNIT}"
)
# The shapes `hosts/lib/reconcile_lib/ufw.py` reads.
UFW_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
UFW_SPEC = re.compile(r"(?:allow|deny|limit|reject) [A-Za-z0-9 ./:_-]+")
UFW_RULE_KEYS = frozenset({"id", "spec", "keep", "position", "before"})
UFW_LEGACY_KEYS = frozenset({"spec", "comment", "replaced_by"})
# The keys a package or component spec may carry for an arm, and the shapes the other arms read.
ARM_KEYS = frozenset({"ufw", "libvirt", "users", "lines"})
LIBVIRT_DIR = "/usr/lib/homelab/libvirt"
LIBVIRT_ROOTS = {"networks": "network", "nwfilters": "filter"}
LIBVIRT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SYSUSERS = "/usr/lib/sysusers.d/homelab.conf"
USER_KEYS = frozenset({"name", "uid", "home", "shell", "comment"})
USER_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
LINE_KEYS = ({"path", "line", "regexp"}, {"path", "block", "marker"})
OCTAL = re.compile(r"[0-7]{3,4}")


class Package(Protocol):
    """The part of `host.Package` these functions write, named so neither module imports the other."""

    name: str
    files: dict[str, tuple[bytes, int]]
    activations: dict[str, str]
    ufw_rules: list[dict]
    ufw_legacy: list[dict]
    libvirt: dict[str, list[dict]]
    users: list[dict]
    lines: list[dict]


def add_activation(package: Package, directory: Path, dst: str, action: object) -> None:
    """Record `action` for the file at `dst`; None records nothing.

    Raises:
        RenderRefused: `action` is not one reconcile knows.
    """
    if action is None:
        return
    if not isinstance(action, str) or not ACTION.fullmatch(action):
        raise RenderRefused(
            f"{directory}: {dst} activate {action!r} is not none, sysctl, modules-load, "
            "reboot, or try-restart, reload or manual followed by :<unit>"
        )
    package.activations[dst] = action


def add_ufw(
    package: Package,
    directory: Path,
    section: object,
    render: Callable[[str], str],
) -> None:
    """Record the rules and legacy rules a `ufw:` map declares, with each value rendered.

    Raises:
        RenderRefused: The map is malformed, or a value is one the arm would refuse.
    """
    if not isinstance(section, dict) or not set(section) <= {"rules", "legacy"}:
        raise RenderRefused(
            f"{directory}: ufw {section!r} is not a map of rules and legacy"
        )
    for entry in section.get("rules") or []:
        if (
            not isinstance(entry, dict)
            or not {"id", "spec"} <= set(entry) <= UFW_RULE_KEYS
        ):
            raise RenderRefused(
                f"{directory}: ufw rule {entry!r} needs id and spec only"
            )
        rule = {
            "id": entry["id"],
            "spec": render(str(entry["spec"])),
            "keep": entry.get("keep", False),
            "position": entry.get("position", "last"),
            "before": entry.get("before"),
        }
        if not (
            isinstance(rule["id"], str)
            and UFW_ID.fullmatch(rule["id"])
            and UFW_SPEC.fullmatch(rule["spec"])
            and isinstance(rule["keep"], bool)
            and rule["position"] in ("first", "last")
        ):
            raise RenderRefused(f"{directory}: ufw rule {rule!r} is malformed")
        package.ufw_rules.append(rule)
    for entry in section.get("legacy") or []:
        if (
            not isinstance(entry, dict)
            or not {"spec", "replaced_by"} <= set(entry) <= UFW_LEGACY_KEYS
        ):
            raise RenderRefused(
                f"{directory}: ufw legacy rule {entry!r} needs spec and replaced_by"
            )
        legacy = {
            "spec": render(str(entry["spec"])),
            "comment": render(str(entry.get("comment", ""))),
            "replaced_by": entry["replaced_by"],
        }
        if not UFW_SPEC.fullmatch(legacy["spec"]) or "'" in legacy["comment"]:
            raise RenderRefused(f"{directory}: ufw legacy rule {legacy!r} is malformed")
        package.ufw_legacy.append(legacy)


def ufw_section(name: str, rules: list[dict], legacy: list[dict]) -> dict | None:
    """The manifest's `ufw` object, or None when the package declares nothing.

    Raises:
        RenderRefused: Two rules share an id, a `before:` or a `replaced_by` names no rule.
    """
    if not rules and not legacy:
        return None
    ids = [rule["id"] for rule in rules]
    if len(ids) != len(set(ids)):
        raise RenderRefused(f"{name}: two ufw rules share an id")
    named = [rule["before"] for rule in rules if rule["before"]]
    named += [entry["replaced_by"] for entry in legacy]
    if set(named) - set(ids):
        raise RenderRefused(
            f"{name}: ufw before and replaced_by name {sorted(set(named) - set(ids))}, "
            "which no rule declares"
        )
    return {"rules": rules, "legacy": legacy}


def add_arms(
    package: Package,
    directory: Path,
    arms: dict,
    render: Callable[[str], str],
    render_file: Callable[[str], str],
) -> None:
    """Record each arm's declarations from a spec's `ufw`, `libvirt`, `users` and `lines` keys.

    Args:
        render: Renders one string with the host's context.
        render_file: Renders one template of `directory` with the host's context.

    Raises:
        RenderRefused: A declaration is malformed, or one the arm would refuse.
    """
    if "ufw" in arms:
        add_ufw(package, directory, arms["ufw"], render)
    if "libvirt" in arms:
        add_libvirt(package, directory, arms["libvirt"], render_file)
    if "users" in arms:
        add_users(package, directory, arms["users"])
    if "lines" in arms:
        add_lines(package, directory, arms["lines"], render)


def has_arms(package: Package) -> bool:
    """Whether the package declares state for any arm."""
    return bool(
        package.ufw_rules
        or package.ufw_legacy
        or any(package.libvirt.values())
        or package.users
        or package.lines
    )


def _checked_xml(directory: Path, kind: str, name: str, xml: str) -> None:
    """Refuse XML that is not a `kind` object named `name` with a pinned UUID."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RenderRefused(f"{directory}: libvirt {name} is not XML: {exc}") from exc
    uuid = (root.findtext("uuid") or "").strip()
    if root.tag != LIBVIRT_ROOTS[kind] or root.findtext("name") != name:
        raise RenderRefused(
            f"{directory}: libvirt {kind} {name}: the XML root must be "
            f"<{LIBVIRT_ROOTS[kind]}> with the declared <name>"
        )
    if not UUID.fullmatch(uuid):
        raise RenderRefused(
            f"{directory}: libvirt {kind} {name} pins no <uuid>; net-define would "
            "collide with the live object under another one"
        )


def add_libvirt(
    package: Package,
    directory: Path,
    section: object,
    render_file: Callable[[str], str],
) -> None:
    """Install each declared object's XML and record it for the libvirt arm.

    Raises:
        RenderRefused: The map is malformed, or an object's XML is not what the arm defines.
    """
    if not isinstance(section, dict) or not set(section) <= set(LIBVIRT_ROOTS):
        raise RenderRefused(
            f"{directory}: libvirt {section!r} is not a map of networks and nwfilters"
        )
    for kind in LIBVIRT_ROOTS:
        for entry in section.get(kind) or []:
            if not (
                isinstance(entry, dict)
                and set(entry) == {"name", "src"}
                and isinstance(entry["name"], str)
                and LIBVIRT_NAME.fullmatch(entry["name"])
            ):
                raise RenderRefused(
                    f"{directory}: libvirt {kind} entry {entry!r} needs a plain name and a src"
                )
            name, dst = entry["name"], f"{LIBVIRT_DIR}/{kind}/{entry['name']}.xml"
            xml = render_file(str(entry["src"]))
            _checked_xml(directory, kind, name, xml)
            if dst in package.files:
                raise RenderRefused(f"{package.name}: two files install {dst}")
            package.files[dst] = (xml.encode(), 0o644)
            package.libvirt[kind].append({"name": name, "path": dst})


def add_users(package: Package, directory: Path, users: object) -> None:
    """Record each declared user for the users arm and for the sysusers.d file.

    Raises:
        RenderRefused: An entry is malformed, or a name or a uid is declared twice.
    """
    if not isinstance(users, list):
        raise RenderRefused(f"{directory}: users {users!r} is not a list")
    for user in users:
        if not (
            isinstance(user, dict)
            and set(user) == USER_KEYS
            and isinstance(user["name"], str)
            and USER_NAME.fullmatch(user["name"])
            and type(user["uid"]) is int
            and 0 < user["uid"] < 65534
            and all(
                isinstance(user[key], str)
                and user[key].startswith("/")
                and ".." not in user[key]
                and not re.search(r"\s", user[key])
                for key in ("home", "shell")
            )
            and isinstance(user["comment"], str)
            and not set('"\n\\') & set(user["comment"])
        ):
            raise RenderRefused(f"{directory}: user {user!r} is malformed")
        if any(
            user[key] == seen[key] for seen in package.users for key in ("name", "uid")
        ):
            raise RenderRefused(
                f"{package.name}: user {user['name']} or its uid is declared twice"
            )
        package.users.append(user)


def add_sysusers(package: Package) -> None:
    """Install the sysusers.d file for the package's users; nothing without users.

    Raises:
        RenderRefused: A file already installs it.
    """
    if not package.users:
        return
    if SYSUSERS in package.files:
        raise RenderRefused(f"{package.name}: {SYSUSERS} is the rendered users list")
    text = "".join(
        f'u {u["name"]} {u["uid"]} "{u["comment"] or "-"}" {u["home"]} {u["shell"]}\n'
        for u in sorted(package.users, key=lambda u: u["uid"])
    )
    package.files[SYSUSERS] = (text.encode(), 0o644)


def _matches(pattern: str, text: str) -> bool:
    """Whether `pattern` compiles and matches `text`, which keeps a rerun from adding a copy."""
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


def add_lines(
    package: Package, directory: Path, edits: object, render: Callable[[str], str]
) -> None:
    """Record each declared line or block edit and its `activate:` action, values rendered.

    Raises:
        RenderRefused: An edit is malformed or one the arm would refuse, or two edits of one
            path name different actions.
    """
    if not isinstance(edits, list):
        raise RenderRefused(f"{directory}: lines {edits!r} is not a list")
    for entry in edits:
        if (
            not isinstance(entry, dict)
            or set(entry) - {"mode", "activate"} not in LINE_KEYS
        ):
            raise RenderRefused(
                f"{directory}: lines entry {entry!r} needs path with line and regexp, "
                "or path with block and marker"
            )
        edit = {k: render(str(v)) for k, v in entry.items() if k != "activate"}
        text = edit.get("line") or edit.get("block") or ""
        anchor = edit.get("regexp") or edit.get("marker") or ""
        shaped = (
            "{mark}" in anchor and "\n" not in anchor
            if "block" in edit
            else "\n" not in text and _matches(anchor, text)
        )
        if not (
            edit["path"].startswith("/")
            and ".." not in edit["path"].split("/")
            and text
            and anchor
            and OCTAL.fullmatch(edit.get("mode", "0644"))
            and shaped
        ):
            raise RenderRefused(f"{directory}: lines edit {edit!r} is malformed")
        action = entry.get("activate")
        if (
            action is not None
            and package.activations.get(edit["path"], action) != action
        ):
            raise RenderRefused(
                f"{package.name}: two lines edits of {edit['path']} name different actions"
            )
        add_activation(package, directory, edit["path"], action)
        package.lines.append(edit)


def arm_sections(package: Package) -> dict[str, dict]:
    """The manifest section of each arm the package declares state for, by arm name."""
    sections = {}
    ufw = ufw_section(package.name, package.ufw_rules, package.ufw_legacy)
    if ufw:
        sections["ufw"] = ufw
    if any(package.libvirt.values()):
        sections["libvirt"] = {
            kind: sorted(objects, key=lambda o: o["name"])
            for kind, objects in package.libvirt.items()
        }
    if package.users:
        sections["users"] = {"users": sorted(package.users, key=lambda u: u["uid"])}
    if package.lines:
        sections["lines"] = {"edits": package.lines}
    return dict(sorted(sections.items()))
