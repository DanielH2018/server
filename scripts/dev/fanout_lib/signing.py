"""The signing-key gate: refuse a placement host whose commit signatures GitHub rejects.

This repo's branch protection requires verified signatures. An agent launched on a host whose
SSH signing key is not registered on the GitHub account signs commits GitHub reads as
`verified=false reason=unknown_key`, so its PR cannot merge until someone re-signs the branch
by hand (issue #1615, observed on PR #1572).

The comparison is against what GitHub actually accepts — the account's registered signing
keys, read live from the API — never a fingerprint written down here, which would go stale the
moment a key is rotated.

Key material never reaches a message: `user.signingkey` may legally name a PRIVATE key file,
and on a host configured that way the read returns that file's contents. Every refusal here
names the host and an `SHA256:` fingerprint instead.
"""

import base64
import hashlib
import json
import re
import subprocess

GH_TIMEOUT_S = 30.0
# An OpenSSH public key line: type, base64 blob, then an optional comment this ignores —
# the comment is per-host text ("ubuntu@daniel-server-ansible") and no part of the identity
# GitHub matches on. The three type prefixes are every family GitHub accepts as a signing
# key: `ssh-ed25519`/`ssh-rsa`, `ecdsa-sha2-nistp256`, and the `sk-` hardware-backed pair. A
# rotation to a type this missed would empty the registered set and refuse every host with a
# misleading reason, so the prefixes are named rather than left as `ssh-`. They stay anchored
# rather than widened to any word: a bare `[A-Za-z0-9@.-]+` type would read `-----BEGIN` as a
# key type and let a private-key read through.
PUBLIC_KEY_RE = re.compile(
    r"^((?:ssh|ecdsa|sk)-[A-Za-z0-9@.-]+)\s+([A-Za-z0-9+/]+={0,3})(?:\s|$)"
)


def signing_key_read_command(repo: str) -> str:
    """The read-only shell command that prints a host's commit-signing public key.

    Resolves `user.signingkey` the way git does for a commit made in `repo`: a literal
    `ssh-…` value is printed as-is, anything else is treated as a file path with a leading
    `~` expanded. An unset or unreadable value prints nothing, which the caller's parse
    refuses — the gate fails closed rather than guessing.
    """
    return (
        f"v=$(git -C {repo} config --get user.signingkey); "
        'case "$v" in '
        'ssh-*) printf "%s\\n" "$v" ;; '
        '*) cat "$(printf "%s" "$v" | sed "s|^~|$HOME|")" ;; '
        "esac"
    )


def normalize_key(text: str | None) -> str | None:
    """The `<type> <blob>` identity of the first OpenSSH public key line in `text`, else None.

    Drops the comment field so the same key read from a host's file and from GitHub's API
    compare equal. Returns None for anything that is not a public key line — an empty read,
    an error message, or a private key file — so a caller can never match None against None
    and call it verified.
    """
    for line in (text or "").splitlines():
        match = PUBLIC_KEY_RE.match(line.strip())
        if match:
            return f"{match.group(1)} {match.group(2)}"
    return None


def fingerprint(normalized_key: str) -> str:
    """The `SHA256:…` fingerprint of a normalized key, matching `ssh-keygen -l`.

    Returns `SHA256:<unreadable>` when the blob does not decode, so a message-building
    caller cannot raise on malformed input.
    """
    try:
        blob = base64.b64decode(normalized_key.split()[1], validate=True)
    except ValueError, IndexError:
        return "SHA256:<unreadable>"
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _gh(args: list[str]) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, timeout=GH_TIMEOUT_S
    ).stdout


def registered_signing_keys() -> frozenset[str]:
    """The normalized signing keys GitHub verifies commits against for this account.

    Reads the authenticated login, then that user's public `ssh_signing_keys` list. The
    public per-user endpoint is deliberate: `/user/ssh_signing_keys` needs the
    `admin:ssh_signing_key` scope the repo's token does not carry, and the public list is the
    same set GitHub checks a commit signature against.

    Raises:
        subprocess.CalledProcessError: `gh` exited non-zero (no auth, no network).
        subprocess.TimeoutExpired: `gh` did not answer within GH_TIMEOUT_S.
        ValueError: the reply was not the expected JSON shape. `json.JSONDecodeError` is a
            ValueError, so one except clause covers both.
    """
    login = _gh(["api", "/user", "--jq", ".login"]).strip()
    if not login:
        raise ValueError("gh api /user returned no login")
    keys = json.loads(_gh(["api", f"/users/{login}/ssh_signing_keys"]))
    normalized = {normalize_key(entry.get("key")) for entry in keys}
    return frozenset(key for key in normalized if key)


def unverified_reason(
    host: str, key_text: str | None, registered: frozenset[str]
) -> str | None:
    """Why `host` must not be placed on, or None when GitHub verifies its signatures.

    Args:
        host: the candidate placement host.
        key_text: what `signing_key_read_command` printed there.
        registered: the account's registered signing keys, from `registered_signing_keys`.

    Returns:
        None when the host's key is one GitHub accepts, else a one-line reason naming the
        host and the key's fingerprint — never the key itself.
    """
    key = normalize_key(key_text)
    if key is None:
        return (
            f"{host}: read no SSH public signing key from its git config, so whether GitHub "
            "verifies its commits cannot be established — set user.signingkey to an "
            "`ssh-…` public key there"
        )
    if key not in registered:
        return (
            f"{host}: its commit-signing key ({fingerprint(key)}) is not registered as a "
            "signing key on the GitHub account, so every commit it signs reads "
            "verified=false reason=unknown_key and its PR cannot merge past the "
            "verified-signatures rule — register that key, or place the batch elsewhere"
        )
    return None
