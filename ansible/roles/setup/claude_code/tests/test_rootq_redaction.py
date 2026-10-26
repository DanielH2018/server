"""rootq's output filter as a case table: what tier 2 must hide and what it must keep (#6351).

The filter redacts by default. A value prints only when it matches one of the token classes in
homelab_rootq.py, and a value under a credential-named key never prints. MUST_REDACT holds
every leak a review measured on 70f8b7c65, the property cases throw random high-entropy
tokens at every value position, and the must-pass cases hold real nft, ufw and sshd -T output
readable.

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_rootq_redaction.py
"""

import random
import string

import homelab_rootq as rootq
import pytest


def shown(text: str, verb: str | None = None) -> str:
    """What the filter prints for TEXT; a refusal prints nothing, so it hides the secret too."""
    try:
        if verb is None:
            return rootq.filter_output(text)
        return rootq.filter_output(text, verb)
    except rootq.Refused:
        return ""


# ── Must redact: each leak measured on 70f8b7c65, with the substring that must not print ──
# Split so the fixture does not read as a real credential to gitleaks.
WG_PSK = "yAnz5TF+lXXJte14" + "tji3zlMNq+hd2rYUIgJBgB3fBmk="
MUST_REDACT = [
    ("PresharedKey = " + WG_PSK, WG_PSK),
    ("psk = abcdefgh12", "abcdefgh12"),
    ("pw: hunter22hunter", "hunter22hunter"),
    ("auth: hunter22hunter", "hunter22hunter"),
    ("AUTH = hunter22hunter", "hunter22hunter"),
    ("jwt: eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ("creds: admin/hunter22", "hunter22"),
    ("0 * * * * sshpass -p hunter22 ssh x", "hunter22"),
    ("sshpass -p hunter22 ssh x", "hunter22"),
    ("@daily docker login -p hunter22 reg", "hunter22"),
    ("docker login -p hunter22 reg", "hunter22"),
    ("ExecStart=/usr/bin/x -p hunter22", "hunter22"),
    ("machine x login u password hunter22", "hunter22"),
    ("PASSWORD=84726193", "84726193"),
    ("PIN=84726193", "84726193"),
    ("api_secret = 12345678", "12345678"),
    ("TOKEN=1234567890", "1234567890"),
    ('Environment="PIN=84726193"', "84726193"),
]


@pytest.mark.parametrize("text, secret", MUST_REDACT)
def test_a_measured_leak_is_redacted(text, secret) -> None:
    assert secret not in shown(text + "\n")


# ── Property: random tokens in every value position ──────────────────────────────────────
CREDENTIAL_KEYS = [
    "password",
    "PIN",
    "api_secret",
    "Token",
    "PresharedKey",
    "auth",
    "jwt",
]
OTHER_KEYS = ["MODE", "level", "name", "retries", "target", "Description"]
KEY_POSITIONS = ["{k}={v}", "{k} = {v}", '{k}="{v}"', "{k}: {v}", "  {k}: '{v}'"]
COMMAND_POSITIONS = [
    "- {v}",
    "0 3 * * * /usr/bin/x --{k} {v}",
    "0 3 * * * /usr/bin/x --{k}={v}",
    "0 3 * * * /usr/bin/x -p {v} /var/log/x",
    "*/5 * * * * /usr/bin/x {v} >/dev/null 2>&1",
    "@reboot {k}={v} /usr/bin/x",
    'Environment="{k}={v}"',
]
ALPHABETS = {
    "digits": string.digits,
    "base64": string.ascii_letters + string.digits + "+/",
    "hex": "0123456789abcdef",
    "mixed": string.ascii_letters + string.digits + "._-~",
}


def tokens(per_kind: int = 40) -> list[str]:
    rng = random.Random(6351)
    out = []
    for alphabet in ALPHABETS.values():
        for _ in range(per_kind):
            token = "".join(rng.choice(alphabet) for _ in range(rng.randint(8, 64)))
            out.append(token + ("=" * rng.randint(0, 2) if "+" in alphabet else ""))
    return out


TOKENS = tokens()


@pytest.mark.parametrize("template", KEY_POSITIONS + COMMAND_POSITIONS)
def test_no_random_token_prints_in_any_value_position(template) -> None:
    rng = random.Random(template)
    for token in TOKENS:
        key = rng.choice(CREDENTIAL_KEYS + OTHER_KEYS)
        out = shown(template.format(k=key, v=token) + "\n")
        assert token not in out, (template, key, token, out)


@pytest.mark.parametrize("template", KEY_POSITIONS)
def test_a_redacted_non_credential_value_keeps_its_key(template) -> None:
    # A filter that refused everything would pass the property above; this one would not.
    for key in OTHER_KEYS:
        out = shown(template.format(k=key, v=TOKENS[0]) + "\n")
        assert key in out and "<value>" in out, (template, out)


STRUCTURED_POSITIONS = [
    ("sshd", "banner {v}"),
    ("sshd", "allowusers ubuntu {v}"),
    ("nft", "\t\tip saddr {v} accept"),
    ("nft", '\t\ttcp dport 22 accept comment "{v}"'),
    ("ufw", "{v}                     ALLOW IN    Anywhere"),
    ("ufw", "22/tcp                     ALLOW IN    Anywhere    # {v}"),
]


@pytest.mark.parametrize("verb, template", STRUCTURED_POSITIONS)
def test_no_random_token_prints_in_structured_output(verb, template) -> None:
    for token in TOKENS:
        out = shown(template.format(v=token) + "\n", verb)
        assert token not in out, (verb, template, token, out)


# ── Must pass: real output stays readable ────────────────────────────────────────────────
SSHD = """port 22
addressfamily any
listenaddress [::]:22
listenaddress 0.0.0.0:22
permitrootlogin prohibit-password
passwordauthentication no
pubkeyauthentication yes
kbdinteractiveauthentication no
maxauthtries 3
logingracetime 120
clientaliveinterval 300
loglevel INFO
syslogfacility AUTH
authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2
hostkey /etc/ssh/ssh_host_ed25519_key
subsystem sftp /usr/lib/openssh/sftp-server
acceptenv LANG LC_*
maxstartups 10:30:100
persourcepenalties crash:90s authfail:5s noauth:1s grace-exceeded:10s max:10m min:15s
ciphers chacha20-poly1305@openssh.com,aes128-ctr,aes256-gcm@openssh.com
kexalgorithms sntrup761x25519-sha512@openssh.com,curve25519-sha256,diffie-hellman-group16-sha512
macs umac-64-etm@openssh.com,hmac-sha2-256-etm@openssh.com
hostkeyalgorithms ssh-ed25519-cert-v01@openssh.com,ssh-ed25519,rsa-sha2-512
"""
NFT = """table inet filter {
\tchain input {
\t\ttype filter hook input priority filter; policy drop;
\t\tct state established,related accept
\t\tiifname "lo" accept
\t\ttcp dport { 22, 80, 443 } accept
\t\tip saddr 192.168.1.0/24 tcp dport 9100 counter packets 123456789 bytes 98765432100 accept
\t\tip6 saddr fe80::/64 udp dport 546 accept
\t\tiifname "wg0" meta mark set 0x4000 jump ufw-before-input
\t}
}
"""
UFW = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     LIMIT IN    Anywhere
80,443/tcp                 ALLOW IN    192.168.1.0/24
9100 on eth0               ALLOW IN    10.42.0.0/16
22/tcp (v6)                LIMIT IN    Anywhere (v6)
"""


@pytest.mark.parametrize("verb, text", [("sshd", SSHD), ("nft", NFT), ("ufw", UFW)])
def test_real_structured_output_prints_unchanged(verb, text) -> None:
    assert rootq.filter_output(text, verb) == text


def test_a_kube_chain_keeps_its_named_segments_and_hides_its_hash() -> None:
    out = rootq.filter_output("\t\tjump KUBE-POD-FW-4W7QLFUGB5UMKUIR\n", "nft")
    assert out == "\t\tjump KUBE-POD-FW-<word>\n"


def test_netplan_keeps_its_structure_and_plain_values() -> None:
    text = (
        "# static address\nnetwork:\n  version: 2\n  renderer: networkd\n  ethernets:\n"
        "    eth0:\n      dhcp4: false\n      addresses: [192.168.1.10/24]\n"
        "      routes:\n        - to: default\n          via: 192.168.1.1\n"
        "      nameservers:\n        addresses: [1.1.1.1, 2606:4700:4700::1111]\n"
    )
    assert rootq.filter_output(text, "cat").splitlines() == [
        "# <comment>",
        "network:",
        "  version: 2",
        "  renderer: networkd",
        "  ethernets:",
        "    eth0:",
        "      dhcp4: false",
        "      addresses: [192.168.1.10/24]",
        "      routes:",
        "        - to: <value>",
        "          via: 192.168.1.1",
        "      nameservers:",
        "        addresses: [1.1.1.1, 2606:4700:4700::1111]",
    ]


def test_a_crontab_keeps_schedules_binaries_flags_and_paths() -> None:
    text = (
        "SHELL=/bin/sh\nPATH=/usr/local/bin:/usr/bin\nMAILTO=root\n"
        "17 3 * * * /usr/local/bin/registry-gc.sh --dry-run --keep=5 >/dev/null 2>&1\n"
        "@reboot cd /srv/app && ./start mode\nnot a cron line\n"
    )
    assert rootq.filter_output(text).splitlines() == [
        "SHELL=/bin/sh",
        "PATH=/usr/local/bin:/usr/bin",
        "MAILTO=<value>",
        "17 3 * * * /usr/local/bin/registry-gc.sh --dry-run --keep=<v> >/dev/null 2>&1",
        "@reboot cd /srv/app && <cmd> <arg>",
        "<line>",
    ]
