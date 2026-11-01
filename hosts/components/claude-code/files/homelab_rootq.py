#!/usr/bin/python3 -I
"""homelab-rootq — read root-only host state, run as root through one NOPASSWD line (#6323).

Installed as /usr/local/sbin/homelab-rootq by tasks/rootq.yml; /usr/local/bin/rootq runs it
through `sudo -n`. It reads and never writes. Every verb, path and argument shape it accepts
is listed here, and anything not listed is refused. The verbs come in two tiers.

Tier 1 never prints file content. Each takes a PATH on the PATHS list:

  rootq stat PATH           mode, type, owner, group, size and mtime, from lstat; for a
                            DENY path, mode, type, owner, group and `deny=yes` only
  rootq sha256 PATH         the file's SHA-256
  rootq matches PATH FILE   exit 0 when PATH is byte-identical to FILE in your checkout, else 1

For `matches`, the shim hashes FILE as the caller and passes only the hash, so this script
receives `matches PATH HEX` and never opens a caller-named file. The shim also takes the
64-hex SHA-256 in place of FILE, which is how `probe.py applied` asks on a host with no
checkout.

Tier 2 prints, and only from fixed sources, each through the redaction filter:

  rootq nft                 nft list ruleset
  rootq ufw                 ufw status verbose
  rootq sshd                sshd -T
  rootq crontab             crontab -l -u root
  rootq cat PATH            a file on the CAT list, /etc/netplan/*.yaml only

PATH is absolute, and it must resolve to itself: a symlink anywhere along it, `..`, `.`, `//`
or a trailing slash is refused. It must match a PATHS entry, every directory above it must be
root-owned and writable by root alone, and it must miss every DENY pattern. The one exception
is `stat` of a DENY path PATHS names, which answers without size or mtime (#6369).

Output is filtered twice. Output that matches REFUSE, or that gives a credential-named key a
literal value, is withheld whole. The rest is redacted by default: every value is replaced by
a placeholder unless it matches one of the token classes below, and a value under a key whose
name could name a credential is always replaced. Output stops at OUT_MAX bytes.

It runs under /usr/bin/python3, Ubuntu 24.04's 3.12, not uv's pinned interpreter: that one sits
under a user's home, and root never executes a file a user can write. `-I` keeps PYTHONPATH,
the user site and the working directory out of the interpreter.

Exit codes: 0 the verb ran, or `matches` matched; 1 its command exited non-zero, or `matches`
differed; 2 the request was refused; 3 the host lacks the command.
"""

from __future__ import annotations

import fnmatch
import grp
import hashlib
import hmac
import ipaddress
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import time

# Tier 2's commands, each verb's whole argv.
COMMANDS: dict[str, list[str]] = {
    "nft": ["/usr/sbin/nft", "list", "ruleset"],
    "ufw": ["/usr/sbin/ufw", "status", "verbose"],
    "sshd": ["/usr/sbin/sshd", "-T"],
    "crontab": ["/usr/bin/crontab", "-l", "-u", "root"],
}
TIER1 = ("stat", "sha256", "matches")

# DECIDED: the allowlist is deliberately minimal: the files #6323's sessions needed and the
# operator named on 2026-10-25. To add a file, add an entry here plus a must-pass case and a
# must-refuse neighbour case in tests/test_rootq.py. Each entry is a directory and a glob over
# one file name, so `*` never crosses `/`, and the match runs on the resolved path.
# /var/lib/gitops-deploy's files are not here, though the operator named them: sys_user owns
# that directory, so the caller already reads them without root, and the ancestor-owner rule
# below would refuse every path in it anyway.
PATHS = (
    ("/etc/netplan", "*.yaml"),
    ("/etc/cron.d", "*"),
    ("/etc/ufw", "user.rules"),
    ("/etc/ufw", "user6.rules"),
    ("/usr/local/bin", "registry-gc.sh"),
    ("/etc/systemd/system", "*.service"),
    ("/etc/systemd/system", "*.timer"),
    # The polkit rules this role installs, fanout-alert, land and triage, which `probe.py
    # applied claude_code` read as `unread` on 2026-10-25 (#6357): rules.d is 0750 root.
    ("/etc/polkit-1/rules.d", "50-claude-*.rules"),
    # A DENY path, so only `stat` answers it, and without size or mtime (#6369).
    ("/etc/claude-rc", "alert-webhook.env"),
)
# The only files tier 2 prints. A new printing source needs a structural argument for why the
# filter redacts it cleanly: netplan is YAML, whose every line is a key, a list item or a
# comment, so no line reaches the output except as a key and a plain value.
CAT = (("/etc/netplan", "*.yaml"),)
# Refused even when a list above admits the path. Matched against the path and each of its
# ancestors, and fnmatch's `*` crosses `/`. An independent second layer.
DENY = (
    "/etc/shadow*",
    "/etc/gshadow*",
    "/etc/sudoers*",
    "/root/.ssh",
    "/home/*/.ssh",
    "/etc/ssh/*_key",
    "*.env",
    "/etc/rancher/k3s/*",
    "/var/lib/rancher/k3s/server/token",
    "*/node-token",
    "*/cred",
    "*/credentials*",
    "*/keys.txt",
    "*.agekey",
    "*/sops/age",
    "/var/lib/kubelet/pods",
    "*/.netrc",
    "*.netrc",
    "*/wg*.conf",
    "/etc/wireguard",
    "*.psk",
)
# Content that withholds the whole output rather than redacting it. The last four rows are the
# shapes the tree itself renders into the hosts: a Kuma push token in a URL path, netplan's
# Wi-Fi `password:`, a healthchecks ping UUID, and a secret passed as the next word after its
# flag. A `${VAR}` reference in those places names a secret without holding it, so it passes.
REFUSE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|AGE-SECRET-KEY-"
    r"|ENC\[AES256_GCM,"
    r"|(?i:authorization:)"
    r"|(?i:\bbearer\s+[A-Za-z0-9._~+/-])"
    r"|discord(?:app)?\.com/api/webhooks/"
    r"|://[^/\s:@'\"]+:[^/\s@'\"]+@"
    r"|\bK10[0-9a-f]{16,}::"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_"
    r"|\bxox[abprs]-|\bsk-ant-|\bglpat-"
    r"|/api/push/[A-Za-z0-9]"
    r"|(?im:^\s*(?:password|passwd|psk|private-key|private_key|secret|token)\s*:\s*[^\s$])"
    r"|hc-ping\.com/[0-9a-f]{8}-"
    # A flag starts a word, so sshd's `prohibit-password` is not one.
    r"|(?i:(?<![\w-])--?(?:token|password|passwd|secret|api-?key)[ \t]+[^\s$-])"
    r"|hooks\.slack\.com/services/"
    r"|(?:\s-u|--user)\s+[^\s:$]+:[^\s$]"
    r"|\bmysql\w*\b.*\s-p[^\s$]"
)
# A key whose name says it holds a credential, with a literal value after `=` or `:`. Such
# output is withheld whole; the redaction below would hide the value anyway.
_KEYED = re.compile(
    r"(?<![A-Za-z0-9_.-])([\"']?)([A-Za-z_][A-Za-z0-9_.-]*)\1\s*[:=]\s*"
    r"(?=(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&}\]]*))"
)
_CREDENTIAL_NAME = re.compile(
    r"pass|pwd|secret|token|credential|webhook|cookie|\bdsn\b|apikey|privatekey"
    r"|(?:^|[_.-])(?:api|access|private|secret|signing|auth|client)?[_.-]?key$"
)
PATH_SHAPE = re.compile(r"/[A-Za-z0-9._@+:,=/-]{0,4095}")
HEX64 = re.compile(r"[0-9a-f]{64}")
READ_MAX = 1024 * 1024
OUT_MAX = 256 * 1024
TIMEOUT_S = 30
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

# ── The allowlisted token classes ─────────────────────────────────────────────────────────
# Every value the filter prints matches one of these, or it becomes a placeholder. They live
# in one place so that what can reach the output is read here, not inferred from the code.
NUMBER = re.compile(r"-?\d{1,7}(?:\.\d{1,6})?")  # 8+ digits can be a PIN or an id
DURATION = re.compile(r"\d{1,7}(?:us|ms|s|sec|m|min|h|hr|d|w)")
BOOLEAN = re.compile(r"(?i:yes|no|true|false|on|off)")
# An absolute path under a standard top-level directory. A bare leading `/` is not enough:
# about one random base64 token in 64 starts with one.
FHS_PATH = re.compile(
    r"/(?:(?:etc|usr|var|run|srv|opt|home|root|tmp|dev|proc|sys|bin|sbin|lib|lib64|mnt"
    r"|media|boot|snap)(?:/[A-Za-z0-9._@%+-]*)*)?"
)
# A reference names a value without holding it.
REFERENCE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
HEX_MARK = re.compile(r"0x[0-9a-f]{1,8}")  # nft marks and masks
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
ADDRESS_PORT = re.compile(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([\d.]+):(\d{1,5})")
IFACE = re.compile(
    r"lo|(?:eth|wg|br|cni|docker|tun|tap|virbr|tailscale)\d{1,4}|flannel\.\d{1,4}"
    r"|flannel-wg|(?:en|wl)[ops]\d{1,3}(?:s\d{1,3})?(?:f\d)?|br-[0-9a-f]{12}|veth[0-9a-f]{8}"
)
CRON_FIELD = re.compile(
    r"(?:\*|\d{1,2}(?:-\d{1,2})?)(?:/\d{1,2})?(?:,(?:\*|\d{1,2}(?:-\d{1,2})?)(?:/\d{1,2})?)*"
)
CRON_SPECIAL = frozenset(
    "@reboot @yearly @annually @monthly @weekly @daily @midnight @hourly".split()
)
BINARY = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{0,63}")  # a command run by its bare name
SHORT_FLAG = re.compile(r"-[A-Za-z]")
LONG_FLAG = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")
SHELL_OPS = frozenset("; | || && & > >> < 2> 2>> 2>&1 1>&2 >&2".split())
# A key whose name could name a credential: its value is replaced whatever its shape.
CREDENTIAL_KEY = re.compile(
    r"pass|pw|pin|auth|key|secret|token|cred|jwt|psk|cookie|session|salt|otp|api|private"
    r"|cert|sign|hash|webhook|dsn|login|bearer",
    re.IGNORECASE,
)
NETPLAN_WORDS = frozenset("networkd networkmanager ipv4 ipv6 dhcp4 dhcp6".split())
# nft's keywords, plus the name segments of the chains ufw and kube-router create.
NFT_WORDS = frozenset(
    """table chain set map flowtable element elements type typeof hook priority policy device
    devices flags filter nat route raw mangle security input output forward prerouting
    postrouting ingress egress accept drop reject return jump goto continue queue counter
    packets bytes log prefix level group comment limit rate over burst ct state established
    related new invalid untracked status dnat snat masquerade redirect to from ip ip6 inet arp
    bridge netdev saddr daddr sport dport tcp udp udplite icmp icmpv6 icmpx sctp th meta
    l4proto nfproto protocol nexthdr iif oif iifname oifname iiftype oiftype mark fib addr
    local unicast multicast broadcast interval timeout size dynamic constant auto-merge with
    tcp-reset port-unreachable admin-prohibited host-unreachable echo-request echo-reply
    destination-unreachable time-exceeded parameter-problem nd-neighbor-solicit
    nd-neighbor-advert nd-router-advert nd-router-solicit mld-listener-query
    mld-listener-report mld2-listener-report ipv4_addr ipv6_addr inet_service inet_proto
    ifname ether_addr second minute hour day week ether vlan id length hoplimit ttl and or
    xor not != == < > <= >= & | ^ { } kube router pod fw nwplcy services svc sep nodeports
    external firewall proxy masq hostports flannel cni canary default ufw ufw6 before after
    user logging track skip policy allow deny not-local""".split()
)
UFW_WORDS = frozenset(
    """status active inactive logging on off low medium high full default deny allow reject
    limit incoming outgoing routed disabled new profiles skip to action from -- ------ ----
    in out fwd anywhere v6 tcp udp any openssh""".split()
)
SSHD_WORDS = frozenset(
    """yes no any none all inet inet6 info verbose debug debug1 debug2 debug3 quiet fatal
    error auth authpriv daemon user local0 local1 local2 local3 local4 local5 local6 local7
    prohibit-password without-password forced-commands-only sandbox delayed lowdelay
    throughput reliability af11 af12 af13 af21 af22 af23 af31 af32 af33 af41 af42 af43 cs0
    cs1 cs2 cs3 cs4 cs5 cs6 cs7 ef le sftp internal-sftp internal .ssh/authorized_keys
    .ssh/authorized_keys2 lang lc_* colorterm no_color crash authfail noauth grace-exceeded
    max-sessions refuseconnection min max max-sources4 max-sources6 overflow overflow6
    permissive deny-all unlimited publickey password keyboard-interactive hostbased
    gssapi-with-mic sha256 md5""".split()
)
# sshd's algorithm names are built from these segments, joined by `-`, with an optional
# @openssh.com or @libssh.org suffix: a closed vocabulary rather than a word shape.
ALG_SEGMENTS = frozenset(
    """ssh ed25519 ed448 ecdsa sha2 sha1 sha256 sha512 nistp256 nistp384 nistp521 cert v01 rsa
    sk dss chacha20 poly1305 aes128 aes192 aes256 ctr cbc gcm umac 64 96 128 etm hmac md5 256 384 512 521
    curve25519 curve448 ecdh diffie hellman group group1 group14 group16 group18 exchange
    sntrup761x25519 mlkem768x25519 3des webauthn""".split()
)
# A counter may run past NUMBER's 7 digits.
COUNTER_WORDS = frozenset({"packets", "bytes"})
EDGE_LEAD = "{}();,\"'"
EDGE_TRAIL = "{}();,\"':"


class Refused(Exception):
    pass


def config(
    environ: dict[str, str], euid: int | None = None
) -> tuple[str, str, frozenset[int]]:
    """The prefix the lists are rooted at, the command directory override and the owner uids."""
    # DECIDED: a test may root the lists under a scratch directory, replace the binaries and
    # add an owner uid, and only when this is not root. Under sudo the euid is 0 and the
    # environment is reset, so a caller can name none of them; as an ordinary user the
    # override reaches nothing the user could not already read. tests/test_rootq.py holds the
    # root case through the euid parameter.
    if (os.geteuid() if euid is None else euid) == 0:
        return "", "", frozenset({0})
    return (
        environ.get("HOMELAB_ROOTQ_ROOT", "").rstrip("/"),
        environ.get("HOMELAB_ROOTQ_BIN", ""),
        frozenset({0, int(environ.get("HOMELAB_ROOTQ_OWNER_UID", "0"))}),
    )


def denied(path: str) -> bool:
    """Whether PATH or any of its ancestors matches a DENY pattern. PATH may be unchecked."""
    probe = path
    while True:
        if any(fnmatch.fnmatchcase(probe, pattern) for pattern in DENY):
            return True
        # dirname stops changing at "/" for an absolute path and at "" for a relative one.
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent


def listed(path: str, entries: tuple[tuple[str, str], ...], prefix: str) -> bool:
    directory, name = os.path.split(path)
    return not name.startswith(".") and any(
        directory == prefix + d and fnmatch.fnmatchcase(name, glob)
        for d, glob in entries
    )


def check_path(
    path: str,
    entries: tuple[tuple[str, str], ...],
    prefix: str,
    owners: frozenset[int],
    verb: str = "",
) -> str:
    # DECIDED: the deny list goes first, so a denied path is refused as denied whatever else
    # is wrong with it. The one exception is `stat` of a DENY path the list names (#6369).
    # The list is tested before the path is resolved, so a DENY path off it draws the same
    # refusal whether or not it exists: there is no existence oracle beyond the allowlist.
    if denied(path) and not (verb == "stat" and listed(path, entries, prefix)):
        tail = " and not on the allowlist" if verb == "stat" else ""
        raise Refused(f"{path} is on the deny list{tail}")
    if not PATH_SHAPE.fullmatch(path):
        raise Refused(
            "PATH must be absolute and use only letters, digits and . _ @ + : , = / -"
        )
    try:
        resolved = os.path.realpath(path, strict=True)
    except OSError:
        raise Refused(f"{path} does not exist") from None
    if resolved != path:
        raise Refused(
            f"{path} resolves to {resolved}: no symlink, ., .., // or trailing /"
        )
    if not listed(path, entries, prefix):
        raise Refused(f"{path} is not on the allowlist; rootq --help names it")
    parent = os.path.dirname(path)
    while True:
        info = os.lstat(parent)
        if info.st_uid not in owners or info.st_mode & 0o022:
            raise Refused(f"{parent} is not owned and writable by root alone")
        if parent == "/":
            return path
        parent = os.path.dirname(parent)


def open_file(path: str) -> int:
    """An fd on PATH, which must still be a regular file with one link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as err:
        raise Refused(f"cannot open {path}: {err.strerror}") from None
    info = os.fstat(fd)
    problem = (
        f"{path} is not a regular file"
        if not stat.S_ISREG(info.st_mode)
        else f"{path} has {info.st_nlink} hard links"
        if info.st_nlink != 1
        else f"{path} is over {READ_MAX} bytes"
        if info.st_size > READ_MAX
        else ""
    )
    if problem:
        os.close(fd)
        raise Refused(problem)
    return fd


def read_file(path: str) -> bytes:
    fd = open_file(path)
    try:
        return os.read(fd, READ_MAX + 1)
    finally:
        os.close(fd)


def describe(path: str) -> str:
    """Tier 1's stat: metadata only, from lstat. A DENY path's size is its secret's length."""
    info = os.lstat(path)
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        owner = str(info.st_uid)
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = str(info.st_gid)
    kind = "file" if stat.S_ISREG(info.st_mode) else "other"
    mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime))
    rest = "deny=yes" if denied(path) else f"size={info.st_size} mtime={mtime}"
    return f"mode={stat.S_IMODE(info.st_mode):04o} type={kind} owner={owner} group={group} {rest}\n"


# ── Redaction ─────────────────────────────────────────────────────────────────────────────


def holds_credential(text: str) -> bool:
    """Whether TEXT matches REFUSE, or gives a credential-named key a literal value."""
    if REFUSE.search(text):
        return True
    for match in _KEYED.finditer(text):
        key, value = match.group(2), match.group(3).strip("\"'")
        if not _CREDENTIAL_NAME.search(key.lower()):
            continue
        # Under four characters is prose, such as `K3S_TOKEN=xxx` in a usage comment. A short
        # value still never prints: the redaction replaces every credential-named value.
        if len(value) < 4 or value[0] in "$<{%" or plain(value):
            continue
        return True
    return False


def _address(core: str) -> bool:
    m = ADDRESS_PORT.fullmatch(core)
    if m:
        core = m.group(1) or m.group(3)
    elif "." not in core and ":" not in core:
        return False
    try:
        ipaddress.ip_network(core, strict=False)
    except ValueError:
        return False
    return True


def plain(value: str) -> bool:
    """Whether VALUE, as one whole value in a key-value line, may print."""
    return bool(
        NUMBER.fullmatch(value)
        or DURATION.fullmatch(value)
        or BOOLEAN.fullmatch(value)
        or REFERENCE.fullmatch(value)
        or all(FHS_PATH.fullmatch(p) for p in value.split(":"))
        or value.lower() in NETPLAN_WORDS
        or MAC.fullmatch(value.lower())
        or IFACE.fullmatch(value)
        or _address(value)
    )


def unquote(value: str) -> str:
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
    return value[1:-1] if quoted else value


def redact_value(key: str, value: str) -> str:
    if not value.strip():
        return value
    if CREDENTIAL_KEY.search(key):
        return "<value>"  # digits, booleans and references included
    inner = unquote(value.strip())
    if inner.startswith("[") and inner.endswith("]"):
        items = [i.strip() for i in inner[1:-1].split(",") if i.strip()]
        return (
            "[" + ", ".join(i if plain(unquote(i)) else "<value>" for i in items) + "]"
        )
    return value if plain(inner) else "<value>"


def redact_command(cmd: str) -> str:
    """A command line reduced to its binaries, flag names, paths and shell operators."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return "<command>"
    out, head = [], True
    for word in words:
        if word in SHELL_OPS:
            out.append(word)
            head = True
            continue
        redirect = re.fullmatch(r"(\d?>>?|<)(.+)", word)
        name, eq, val = word.partition("=")
        if redirect and FHS_PATH.fullmatch(redirect.group(2)):
            out.append(word)
        elif head and eq and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            out.append(f"{name}={redact_value(name, val)}")
        elif head:
            out.append(
                word if FHS_PATH.fullmatch(word) or BINARY.fullmatch(word) else "<cmd>"
            )
            head = False
        elif SHORT_FLAG.fullmatch(word) or LONG_FLAG.fullmatch(word):
            out.append(word)
        elif eq and LONG_FLAG.fullmatch(name):
            out.append(f"{name}={val if FHS_PATH.fullmatch(val) else '<v>'}")
        else:
            out.append(word if FHS_PATH.fullmatch(word) else "<arg>")
    return " ".join(out)


_KV = re.compile(r"(\s*(?:-\s+)?)([A-Za-z_][A-Za-z0-9_.-]*)(\s*=\s*|:(?:\s+|$))(.*)")
_ITEM = re.compile(r"(\s*-\s+)(.*)")


def redact_text_line(line: str) -> str:
    """A crontab or YAML line: a comment, a cron entry, a key and value, or a list item."""
    stripped = line.strip()
    if not stripped:
        return line
    if stripped.startswith("#"):
        return line[: len(line) - len(line.lstrip())] + "# <comment>"
    fields = stripped.split(None, 5)
    if fields[0] in CRON_SPECIAL and len(fields) > 1:
        return f"{fields[0]} {redact_command(stripped.split(None, 1)[1])}"
    if len(fields) == 6 and all(CRON_FIELD.fullmatch(f) for f in fields[:5]):
        return " ".join(fields[:5]) + " " + redact_command(fields[5])
    m = _KV.fullmatch(line)
    # A word ending in `=` with nothing after it can be base64 padding rather than a key, and
    # YAML never writes a list item as `- key=value`, so neither prints as a key.
    if m and "=" in m.group(3) and ("-" in m.group(1) or not m.group(4).strip()):
        return "<line>"
    if m:
        return (
            m.group(1) + m.group(2) + m.group(3) + redact_value(m.group(2), m.group(4))
        )
    m = _ITEM.fullmatch(line)
    if m:
        return m.group(1) + redact_value("", m.group(2))
    # DECIDED: a line with no key, list or cron structure is free text, and free text has no
    # position that says which token is a value, so the whole line becomes `<line>` rather
    # than any of its words printing. unitq makes the same choice.
    return "<line>"


def _atom(core: str, words: frozenset[str], prev: str) -> bool:
    low = core.lower()
    if low.endswith(("@openssh.com", "@libssh.org")):
        low = low.rsplit("@", 1)[0]
    return bool(
        low in words
        or NUMBER.fullmatch(core)
        or DURATION.fullmatch(core)
        or HEX_MARK.fullmatch(low)
        or (prev in COUNTER_WORDS and core.isdigit())
        or FHS_PATH.fullmatch(core)
        or IFACE.fullmatch(core)
        or MAC.fullmatch(low)
        or _address(core)
        or ("-" in low and all(s in ALG_SEGMENTS for s in low.split("-")))
    )


def _core(core: str, words: frozenset[str], prev: str) -> str:
    if not core or _atom(core, words, prev):
        return core
    for sep in (",", ":", "/", "-"):
        if sep in core:
            return sep.join(_core(p, words, prev) for p in core.split(sep))
    return "<word>"


def redact_words(line: str, words: frozenset[str], keyword_first: bool) -> str:
    """A line of nft, ufw or sshd output: each token kept only when it is an allowlisted atom."""
    body, hash_, _ = line.partition("#")
    prev = ""
    out = []
    for i, tok in enumerate(re.split(r"(\s+)", body)):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        core = tok.lstrip(EDGE_LEAD)
        lead = tok[: len(tok) - len(core)]
        stripped = core.rstrip(EDGE_TRAIL)
        trail = core[len(stripped) :]
        if keyword_first and i == 0:
            kept = stripped if re.fullmatch(r"[a-z][a-z0-9]*", stripped) else "<word>"
        else:
            kept = _core(stripped, words, prev)
        out.append(lead + kept + trail)
        prev = stripped.lower()
    return "".join(out) + ("# <comment>" if hash_ else "")


# DECIDED: nft, ufw and sshd -T print in a grammar their own program defines, so a token is
# kept only when it is a keyword of that grammar, an address, a port, an interface name or
# another class above. sshd's option names are fixed by sshd, and none takes a secret, so its
# values are judged by shape rather than by CREDENTIAL_KEY, which would hide
# `passwordauthentication no`, the line a session most often needs.
_STRUCTURED = {
    "nft": (NFT_WORDS, False),
    "ufw": (UFW_WORDS, False),
    "sshd": (SSHD_WORDS, True),
}


def filter_output(text: str, verb: str = "crontab") -> str:
    if holds_credential(text):
        raise Refused(
            "the output matches a credential pattern, so none of it is printed"
        )
    if verb in _STRUCTURED:
        words, keyword_first = _STRUCTURED[verb]
        lines = [redact_words(x, words, keyword_first) for x in text.split("\n")]
    else:
        lines = [redact_text_line(x) for x in text.split("\n")]
    out = "\n".join(lines)
    if len(out.encode()) > OUT_MAX:
        out = (
            out.encode()[:OUT_MAX].decode("utf-8", "ignore")
            + f"\n[truncated at {OUT_MAX} bytes]\n"
        )
    return out


def tier1(verb: str, path: str, args: list[str]) -> int:
    if verb == "stat":
        sys.stdout.write(describe(path))
        return 0
    if verb == "matches" and not HEX64.fullmatch(args[0]):
        raise Refused("matches takes PATH and the 64-hex SHA-256 the shim computed")
    data = read_file(path)
    # A hash of a file that holds a credential is an offline guessing oracle for it, such as
    # the netplan Wi-Fi password, so tier 1 refuses that file just as tier 2 would.
    if holds_credential(data.decode("utf-8", "replace")):
        raise Refused(f"{path} matches a credential pattern, so it is not hashed")
    digest = hashlib.sha256(data).hexdigest()
    if verb == "sha256":
        print(digest)
        return 0
    same = hmac.compare_digest(digest, args[0])
    print("match" if same else "differ")
    return 0 if same else 1


def run_command(verb: str, argv: list[str]) -> int:
    if not os.access(argv[0], os.X_OK):
        print(f"missing command: {argv[0]}", file=sys.stderr)
        return 3
    try:
        result = subprocess.run(
            argv,
            env=ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"{argv[0]} ran past {TIMEOUT_S}s", file=sys.stderr)
        return 1
    sys.stdout.write(filter_output(result.stdout, verb))
    sys.stderr.write(filter_output(result.stderr, verb))
    if result.returncode:
        print(f"{argv[0]} exited {result.returncode}", file=sys.stderr)
        return 1
    return 0


def dispatch(verb: str, args: list[str], environ: dict[str, str]) -> int:
    prefix, bindir, owners = config(environ)
    if verb in COMMANDS:
        if args:
            raise Refused(f"{verb} takes no arguments")
        argv = list(COMMANDS[verb])
        if bindir:
            argv[0] = os.path.join(bindir, os.path.basename(argv[0]))
        return run_command(verb, argv)
    if verb == "cat":
        if len(args) != 1:
            raise Refused("cat takes exactly one PATH")
        path = check_path(args[0], CAT, prefix, owners)
        data = read_file(path)
        if b"\0" in data:
            raise Refused(f"{path} is binary")
        sys.stdout.write(filter_output(data.decode("utf-8", "replace"), "cat"))
        return 0
    if verb in TIER1:
        want = 2 if verb == "matches" else 1
        if len(args) != want:
            raise Refused(
                f"{verb} takes {'PATH HEX' if want == 2 else 'exactly one PATH'}"
            )
        return tier1(verb, check_path(args[0], PATHS, prefix, owners, verb), args[1:])
    raise Refused(
        f"unknown verb {verb!r}; one of {' '.join([*TIER1, *COMMANDS, 'cat'])}"
    )


def main(argv: list[str], environ: dict[str, str]) -> int:
    if argv in (["-h"], ["--help"]):
        print(__doc__.split("\n\n", 1)[1].rstrip())
        print("\nPATHS: " + " ".join(f"{d}/{g}" for d, g in PATHS))
        return 0
    try:
        if not argv:
            raise Refused("usage: rootq VERB [PATH]; rootq --help lists the verbs")
        return dispatch(argv[0], argv[1:], environ)
    except Refused as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv[1:], dict(os.environ)))
