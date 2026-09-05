"""The push-token shape check, over an already-decrypted mapping.

`RotationTools.sops_decrypt` does the decrypting; this module only inspects what it returned,
which is what lets the whole check be tested with synthetic values on a host (or in CI) with
no age key. Nothing here opens a file or spawns a process.
"""

import re

# Uptime Kuma rejects a push token that is not exactly 32 letters/digits, and it rejects it at
# monitor-CREATION time — so the tile is never made and every repo-side gate stays green: the
# template interpolates the bad value fine, the manifest renders, CI passes, and the only
# evidence is a validation error in the autokuma sidecar log. `ruleset_drift_push_token`
# shipped 30 chars in PR #675 and its monitor never existed for a day before anyone noticed.
# `cmd_rotate` mints `token_hex(16)`, which satisfies this, so the auto-rotation path agrees.
PUSH_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{32}$")


def malformed_push_tokens(values: dict) -> list[tuple[str, str]]:
    """Every `*_push_token` whose value Uptime Kuma would refuse, as (name, reason).

    Reasons name the LENGTH and the offending character class, never the value — this feeds a
    Kuma push message and a stdout line, both of which are readable places.

    The `*_push_token` suffix is a complete selector, measured in both directions: every
    secret so named lands in a `push_token` field of uptime-kuma's static-monitors tile, a
    `KUMA_PUSH_*` env var, or an `/api/push/` URL; and every var interpolated into one of
    those three positions is so named (62 of each, no exceptions). A Kuma push token added
    under a different name would be invisible here — grep those three positions if this ever
    needs rechecking.
    """
    bad = []
    for name, value in sorted(values.items()):
        if not name.endswith("_push_token"):
            continue
        if not isinstance(value, str):
            bad.append((name, "not a string"))
        elif not PUSH_TOKEN_RE.match(value):
            bad.append(
                (
                    name,
                    "%d chars, wants 32 letters/digits" % len(value)
                    if len(value) != 32
                    else "32 chars but has a non-alphanumeric character",
                )
            )
    return bad
