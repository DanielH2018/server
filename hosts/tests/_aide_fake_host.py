"""The fake host the aide-check.sh harness runs against: one package, its dpkg records, the
account files, and the host package's manifests.

`_fake_host` writes it under a test's directory; `_aide_check_run._run_check` calls it.
"""

import hashlib
import json

# One installed package: each path's content, and whether dpkg records its digest in md5sums
# (relative, under the pre-usrmerge name) or as a conffile in status.
PACKAGE = {
    "/usr/bin/tool": ("tool v2\n", "md5sums"),
    "/usr/lib/x86_64-linux-gnu/libtool.so": ("lib v2\n", "md5sums"),
    "/etc/tool.conf": ("conf v2\n", "conffile"),
    "/etc/tool.defs": ("# limits\nUMASK\t\t022\nPASS_MAX_DAYS\t99999\n", "conffile"),
    "/usr/share/doc/tool/README": ("readme v1\n", "md5sums"),
    "/usr/lib/systemd/system/tool.service": ("[Unit]\n", "md5sums"),
    "/usr/lib/python3/dist-packages/toolpkg/mod.py": ("x = 1\n", "md5sums"),
}
# Package-owned symlinks, which dpkg lists with no digest, and where each points. The headers
# link points into a package that is not installed, as the kernel headers' rust link does.
PACKAGE_LINKS = {
    "/usr/bin/tool-link": "tool",
    "/usr/src/tool-headers/rust": "../tool-lib-rust/rust",
}
# The host package's links to secrets in the tmpfs its secrets unit fills, which its secrets
# manifest declares: one under /etc, and one under /usr/lib as the Pi's Compose env file sits.
SECRET_LINK = "/etc/svc/config.env"
SECRET_TARGET = "/run/homelab-secrets/etc/svc/config.env"
SYSTEM_SECRET_LINK = "/usr/lib/homelab/compose/compose.env"
SYSTEM_SECRET_TARGET = "/run/homelab-secrets/usr/lib/homelab/compose/compose.env"
# An output whose `destination` is not its path, as the etcd drill's R2 credentials are.
RENAMED_SECRET_LINK = "/etc/homelab/drill-s3.env"
RENAMED_SECRET_TARGET = "/run/homelab-secrets/drill/s3.env"
SECRETS_MANIFEST = "/usr/lib/homelab/secrets.json"
# The host package's apt hook and a script root runs, which dpkg lists with no digest here.
HOMELAB_FILES = ("/etc/apt/apt.conf.d/91homelab-hook", "/usr/lib/homelab/check")
# The user and group databases as the last accepted run copied them: root, a login user, the
# declared claude with the gid its group kept from before the 900-949 band, oldsvc, which the users
# arm no longer declares, and a group the setup script manages.
ACCOUNT_FILES = {
    "/etc/passwd": (
        "root:x:0:0:root:/root:/bin/bash\n"
        "ubuntu:x:1000:1000:Ubuntu:/home/ubuntu:/bin/bash\n"
        "claude:x:900:985:Claude agent:/var/lib/claude:/bin/bash\n"
        "oldsvc:x:907:907:Old svc:/var/lib/oldsvc:/usr/sbin/nologin\n"
    ),
    "/etc/group": (
        "root:x:0:\nubuntu:x:1000:\nclaude:x:985:\noldsvc:x:907:\nssh-users:x:990:ubuntu\n"
    ),
    "/etc/shadow": (
        "root:!*:20000:0:99999:7:::\n"
        "ubuntu:$6$SECRETHASH:20000:0:99999:7:::\n"
        "claude:!*:20000::::::\n"
        "oldsvc:!*:20000::::::\n"
    ),
    "/etc/gshadow": "root:*::\nubuntu:!::\nclaude:!::\noldsvc:!::\nssh-users:!::ubuntu\n",
}
# The host package's lines arm: a block in the package's conffile, as k3s-server adds one to
# /etc/zsh/zshenv, and line edits to another conffile, as host-tuning makes to /etc/login.defs:
# one replaces a shipped line, and two match none and are appended.
BLOCK_MARKER = "# {mark} MANAGED — tool kubeconfig"
LINE_EDITS = [
    {
        "path": "/etc/tool.conf",
        "marker": BLOCK_MARKER,
        "block": "export K=1\nexport L=2\n",
    },
    {"path": "/etc/tool.defs", "line": "UMASK\t\t027", "regexp": "^UMASK\\s"},
    {
        "path": "/etc/tool.defs",
        "line": "PASS_MIN_DAYS\t1",
        "regexp": "^PASS_MIN_DAYS\\s",
    },
    {
        "path": "/etc/tool.defs",
        "line": "PASS_WARN_AGE\t14",
        "regexp": "^PASS_WARN_AGE\\s",
    },
]
# The conffile LINE_EDITS makes line edits to.
DEFS = "/etc/tool.defs"
# Where aide-originals keeps the installed tool version's copy of DEFS, under the directory
# the harness repoints ORIGINALS at.
TOOL_COPY = "tool/1.0/etc/tool.defs"
# The host package's manifest, whose users arm declares claude and a service user svc.
HOST_MANIFEST = "/usr/lib/homelab/manifest.json"
# homelab-reconcile's record, whose users arm has expired oldsvc.
RECONCILE_RECORD = "/var/lib/homelab-reconcile/applied.json"
EXPIRED_USERS = ["oldsvc"]
DECLARED_USERS = [
    {
        "name": "claude",
        "uid": 900,
        "gid": 985,
        "home": "/var/lib/claude",
        "shell": "/bin/bash",
        "comment": "Claude agent",
    },
    {
        "name": "svc",
        "uid": 906,
        "gid": 906,
        "home": "/var/lib/svc",
        "shell": "/usr/sbin/nologin",
        "comment": "Svc",
    },
]


def _dpkg_name(path: str) -> str:
    """The name dpkg records: a package built before usrmerge lists /usr/lib files as /lib."""
    return path.removeprefix("/usr") if path.startswith("/usr/lib/") else path


def _fake_host(root):
    """Writes PACKAGE as installed, with dpkg's records of it, under `root`.

    Returns (dpkg dir, filesystem root). dpkg also records one diversion of the package's README,
    and the filesystem also holds an alternatives link to the package's binary, a unit masked by
    a maintainer script, the usr-merge links, the boot links and GRUB files, a compiled module,
    a unit with its enable link, and one file in each tree nothing packages. A `homelab-host`
    package owns SECRET_LINK, SYSTEM_SECRET_LINK and RENAMED_SECRET_LINK, which SECRETS_MANIFEST
    declares.
    """
    dpkg, fs = root / "dpkg", root / "fs"
    (dpkg / "info").mkdir(parents=True)
    md5sums, conffiles = [], []
    for path, (content, record) in PACKAGE.items():
        target = fs / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        digest = hashlib.md5(content.encode()).hexdigest()
        if record == "md5sums":
            md5sums.append(f"{digest}  {_dpkg_name(path).lstrip('/')}")
        else:
            conffiles.append(f" {path} {digest}")
    for link, target in PACKAGE_LINKS.items():
        (fs / link.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / link.lstrip("/")).symlink_to(target)
    secret_links = {
        SECRET_LINK: SECRET_TARGET,
        SYSTEM_SECRET_LINK: SYSTEM_SECRET_TARGET,
        RENAMED_SECRET_LINK: RENAMED_SECRET_TARGET,
    }
    for link, target in secret_links.items():
        (fs / link.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / link.lstrip("/")).symlink_to(target)
    outputs = [
        {
            "destination": target.removeprefix("/run/homelab-secrets/"),
            "link": link,
            "persist": False,
        }
        for link, target in secret_links.items()
    ]
    (fs / SECRETS_MANIFEST.lstrip("/")).write_text(json.dumps({"outputs": outputs}))
    (fs / HOST_MANIFEST.lstrip("/")).write_text(
        json.dumps({"users": {"users": DECLARED_USERS}, "lines": {"edits": LINE_EDITS}})
    )
    record = fs / RECONCILE_RECORD.lstrip("/")
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"users": {"expired": EXPIRED_USERS}}))
    for path, content in ACCOUNT_FILES.items():
        (fs / path.lstrip("/")).write_text(content)
    (dpkg / "info" / "homelab-host.list").write_text(
        "".join(f"{p}\n" for p in (*secret_links, *HOMELAB_FILES))
    )
    (dpkg / "info" / "tool.list").write_text(
        "".join(f"{_dpkg_name(p)}\n" for p in [*PACKAGE, *PACKAGE_LINKS])
    )
    (dpkg / "info" / "tool.md5sums").write_text("\n".join(md5sums) + "\n")
    # A third-party package, whose md5sums file names each path as ./usr/...
    (fs / "usr/bin/vendor").write_text("vendor v1\n")
    (dpkg / "info" / "vendor.list").write_text("/usr/bin/vendor\n")
    vendor_digest = hashlib.md5(b"vendor v1\n").hexdigest()
    (dpkg / "info" / "vendor.md5sums").write_text(
        f"{vendor_digest}  ./usr/bin/vendor\n"
    )
    (dpkg / "status").write_text(
        "Package: tool\nStatus: install ok installed\nVersion: 1.0\nConffiles:\n"
        + "\n".join(conffiles)
        + "\nDescription: a tool\n"
    )
    # dpkg's diversions file: diverted path, its new name, the diverting package. tool's README
    # now sits at the new name.
    (dpkg / "diversions").write_text(
        "/usr/share/doc/tool/README\n/usr/share/doc/tool/README.distrib\ntool-extra\n"
    )
    (fs / "usr/share/doc/tool/README").rename(fs / "usr/share/doc/tool/README.distrib")
    (fs / "etc" / "alternatives").mkdir(parents=True)
    (fs / "etc" / "alternatives" / "tool").symlink_to(fs / "usr" / "bin" / "tool")
    units = fs / "usr" / "lib" / "systemd" / "system"
    units.mkdir(parents=True, exist_ok=True)
    (units / "tool-cleanup.service").symlink_to("/dev/null")
    # The usr-merge links, as a merged host has them: relative, to the usr counterpart.
    for name in ("bin", "sbin", "lib"):
        (fs / name).symlink_to(f"usr/{name}")
    boot = fs / "boot"
    (boot / "grub").mkdir(parents=True)
    (boot / "initrd.img-7.0.0-42-generic").write_text("initrd\n")
    (boot / "vmlinuz-7.0.0-42-generic").write_text("kernel\n")
    (boot / "initrd.img").symlink_to("initrd.img-7.0.0-42-generic")
    (boot / "vmlinuz").symlink_to("vmlinuz-7.0.0-42-generic")
    (boot / "grub" / "grub.cfg").write_text("menuentry\n")
    (boot / "grub" / "grubenv").write_text("saved_entry=0\n")
    pycache = fs / "usr/lib/python3/dist-packages/toolpkg/__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-312.pyc").write_bytes(b"pyc")
    # The k3s installer's unit, and the link `systemctl enable` makes to it and to a package's.
    system = fs / "etc/systemd/system"
    (system / "multi-user.target.wants").mkdir(parents=True)
    (system / "k3s.service").write_text("[Service]\n")
    (system / "multi-user.target.wants" / "k3s.service").symlink_to("../k3s.service")
    (system / "multi-user.target.wants" / "tool.service").symlink_to(
        "../../../../usr/lib/systemd/system/tool.service"
    )
    for path in (
        "/usr/sbin/implant",
        "/etc/homelab/deployed.env",
        "/etc/sudoers.d/extra",
        "/usr/local/bin/aide-check.sh",
    ):
        (fs / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / path.lstrip("/")).write_text("unpackaged\n")
    return dpkg, fs
