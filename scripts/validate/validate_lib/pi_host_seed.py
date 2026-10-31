"""Host state the real Pi has and the systemd test container lacks (H7.27, H7.25).

The Pi's package edits conffiles of `ufw` (`/etc/ufw/sysctl.conf`) and reloads `ssh.service`
after `sshd -t`. The container installs neither package, so reconcile's line arm fails on the
missing directory and `reload:ssh.service` fails its validation. The seed gives the container
the stock file and an `sshd` that accepts any config, which is what the arm and the validation
meet on the real Pi. The arm and the validation stay strict.

H7.25 adds four more. The Pi's package edits `/etc/hosts` and ships `/etc/resolv.conf`, and the
container runtime bind-mounts both, so a rename onto either fails with EBUSY until they are
unmounted. The package restarts `auditd.service` after it caps the log, and the container has no
auditd, so a stub unit stands in. The node exporter unit listens on the Pi's LAN address, which
the container lacks, so a drop-in swaps its `ExecStart=` for `sleep`; the unit's tmpfiles
`ExecStartPre=` still runs. The Pi already holds the `node_exporter` user, which the users arm
adopts by uid.

H7.30 adds two. The Pi's package declares ufw rules, so reconcile's ufw arm runs. The live Pi's
firewall is active, so the seed installs an active stand-in `ufw`, and an `iptables` that shows the
loopback accept the arm's last step reads. The stand-in keeps its rules in a JSON file and replays
the ufw 0.36.2-6 behaviour docs/host-packages.md records. The real ufw would not do here: enabling
it in the container broke the container's DNS, and every later apt fetch failed to resolve (run
41147933421). The package also edits /etc/postfix/main.cf and restarts postfix, which the
container lacks, so a stub unit and an empty main.cf stand in.
"""

import base64

UFW_SYSCTL = "/etc/ufw/sysctl.conf"
SSHD = "/usr/sbin/sshd"
# The two lines Ubuntu ships uncommented, which the Pi's package rewrites to 1.
UFW_STOCK = "net/ipv4/conf/default/log_martians=0\\nnet/ipv4/conf/all/log_martians=0\\n"
AUDITD_STUB = "[Service]\\nType=oneshot\\nRemainAfterExit=yes\\nExecStart=/bin/true\\n"
EXPORTER_DROPIN = "[Service]\\nExecStart=\\nExecStart=/bin/sleep infinity\\n"
UFW_STUB = "/usr/local/sbin/ufw"
IPTABLES_STUB = "/usr/local/sbin/iptables"
# The stand-in ufw. Appending an existing spec relabels it, `insert` of an existing spec skips
# it, and `delete` with a comment matches only that comment.
UFW_STUB_SOURCE = """#!/usr/bin/python3
import json, os, pathlib, sys

STATE = pathlib.Path(os.environ.get("UFW_STUB_STATE", "/var/lib/ufw-stub.json"))
model = json.loads(STATE.read_text()) if STATE.exists() else {
    "rules": [["limit 22/tcp", ""]],
    "policy": {"incoming": "deny", "outgoing": "allow", "routed": "disabled"},
}
rules, args = model["rules"], sys.argv[1:]
if args[:2] == ["show", "added"]:
    print("Added user rules (see 'ufw status' for running firewall):")
    for spec, comment in rules:
        print("ufw " + spec + (" comment '" + comment + "'" if comment else ""))
    print("(None)") if not rules else None
elif args == ["status", "verbose"]:
    p = model["policy"]
    print("Status: active")
    print(f"Default: {p['incoming']} (incoming), {p['outgoing']} (outgoing), {p['routed']} (routed)")
elif args[:1] == ["status"]:
    print("Status: active")
    for n, (spec, comment) in enumerate(rules, 1):
        print(f"[{n:2}] {spec}" + (f"    # {comment}" if comment else ""))
elif args[:1] == ["default"]:
    model["policy"][args[2]] = args[1]
elif args == ["--force", "enable"]:
    pass
else:
    verb, words, comment = args[0], args[1:], ""
    if "comment" in words:
        at = words.index("comment")
        words, comment = words[:at], words[at + 1]
    if verb == "insert":
        words = words[1:]
    elif verb != "delete":
        words = [verb, *words]
    spec = " ".join(words)
    found = [i for i, (s, c) in enumerate(rules) if s == spec]
    if verb == "delete":
        found = [i for i in found if not comment or rules[i][1] == comment]
        if found:
            del rules[found[0]]
    elif found:
        if verb != "insert":
            rules[found[0]][1] = comment
    elif verb == "insert":
        rules.insert(0, [spec, comment])
    else:
        rules.append([spec, comment])
STATE.write_text(json.dumps(model))
"""
IPTABLES_STUB_SOURCE = "#!/bin/sh\necho '-A ufw-before-input -i lo -j ACCEPT'\n"


def seed_pi_host(c) -> None:
    """Seed in container `c` the host state the live Pi has and the container lacks."""
    for path, source in (
        (UFW_STUB, UFW_STUB_SOURCE),
        (IPTABLES_STUB, IPTABLES_STUB_SOURCE),
    ):
        encoded = base64.b64encode(source.encode()).decode()
        c.ok(f"echo {encoded} | base64 -d > {path} && chmod 0755 {path}")
    c.ok(
        f"mkdir -p /etc/ufw && [ -e {UFW_SYSCTL} ] || printf '{UFW_STOCK}' > {UFW_SYSCTL}"
    )
    c.ok(
        f"[ -e {SSHD} ] || printf '#!/bin/sh\\nexit 0\\n' > {SSHD} && chmod 0755 {SSHD}"
    )
    c.ok(
        "umount /etc/hosts 2>/dev/null || true; umount /etc/resolv.conf 2>/dev/null || true"
    )
    c.ok(f"printf '{AUDITD_STUB}' > /etc/systemd/system/auditd.service")
    c.ok("mkdir -p /etc/systemd/system/node_exporter.service.d")
    c.ok(
        f"printf '{EXPORTER_DROPIN}' > /etc/systemd/system/node_exporter.service.d/10-test.conf"
    )
    c.ok(f"printf '{AUDITD_STUB}' > /etc/systemd/system/postfix.service")
    c.ok("mkdir -p /etc/postfix && touch /etc/postfix/main.cf")
    c.ok("systemctl daemon-reload")
