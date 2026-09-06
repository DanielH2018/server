#!/usr/bin/env python3
"""Mint a Playwright storage-state file holding a logged-in Authelia session.

A browser driven on this host cannot reach the homelab UIs the way a laptop does,
for two reasons this script exists to solve:

  1. Every `*.local.<domain>` route sits behind Authelia's `one_factor` policy
     (`roles/k8s/authelia/templates/config-secret.yaml.j2:100`), so an unauthenticated
     browser sees the login portal instead of the app.
  2. This host's resolver bypasses the LAN DNS, so `.local.<domain>` does not resolve
     to the cluster edge from a shell here. Same trap `core.k8s_endpoint` documents;
     the browser needs Chromium's `--host-resolver-rules`, which `ui_mcp.sh` supplies.

Rather than drive the login form in a browser, this posts straight to Authelia's
first-factor API and converts the returned session cookie into the JSON shape
Playwright's `storageState` expects. That keeps the auth step in Python — matching
every other script here — while the browsing itself stays entirely in Node's
`@playwright/mcp`, so there is only ever one Chromium to install and keep current.

`keepMeLoggedIn` is what makes the file worth saving. The session config sets
`inactivity: '5m'` and `expiration: '1h'`, so an ordinary login would go stale between
two idle minutes of a Claude session; `remember_me: '1M'` applies only when the login
asks for it. Without that flag this file would need re-minting continuously.

Usage:
    uv run python scripts/diagnostics/ui_login.py                  # mint (or refresh) the state file
    uv run python scripts/diagnostics/ui_login.py --check          # ask Authelia if the session still stands
    uv run python scripts/diagnostics/ui_login.py --two-factor     # a two_factor session, ~1h
    uv run python scripts/diagnostics/ui_login.py --path           # print the state file path

The two tiers log in as DIFFERENT users. The one_factor tier is the operator
(`authelia_user`). The two_factor tier is `claude-ui`, an Authelia identity that exists
only for this script — its password and its TOTP shared secret are both SOPS values, and
the code is derived here rather than typed.

That split is what makes the two_factor tier unattended. code-server, n8n and longhorn are
`two_factor` (`roles/k8s/authelia/templates/config-secret.yaml.j2`), so a one_factor cookie
bounces off them, and until 2026-09-06 the only way past that was a code read off the
operator's phone — which meant `test_two_factor_service_serves_its_own_ui` skipped rather
than ran, for eight days at a stretch.

Deriving a code needs the shared secret readable, so the second factor is another value
under the same age key as the first. A dedicated user is what makes that an acceptable
trade: the operator's own enrollment is untouched, revoking Claude's reach into those three
services is deleting one block from `users_database.yml`, and rotating either credential is
a `sops set` plus a deploy. `authelia_k8s_claude_user` in the role defaults carries the
long form.

`--totp <code>` still accepts a typed code, as break-glass for a seeded secret that has
drifted from the row in Authelia's database.

Exit 0 = a usable state file is on disk. Exit 1 = it is missing, expired or rejected.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path as _Path

# `probe_lib` is a namespace package under `scripts/`, so reaching it by package name needs
# `scripts/` on sys.path: a directly-invoked script gets only its own directory, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the import below.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from diagnostics.probe_lib import core

# The cookie Authelia issues for the LAN cookie-domain. Mirrors
# `authelia_k8s_cookie_name` in roles/k8s/authelia/defaults/main.yml:48 — a rename there
# must land here too, which test_ui_login.py asserts against the role default.
COOKIE_NAME = "authelia_session_k8s"

# The two_factor tier's Authelia identity. Mirrors `authelia_k8s_claude_user` in the role
# defaults, the same way COOKIE_NAME mirrors the cookie name, and test_ui_login.py asserts
# the pair still agree.
CLAUDE_USER = "claude-ui"

# The TOTP parameters the seeding task states explicitly when it registers CLAUDE_USER.
# Restating them here rather than relying on either side's defaults is deliberate: a default
# that moved in a later Authelia would desynchronise the two with nothing to show for it but
# a login Authelia rejects.
TOTP_ALGORITHM = hashlib.sha1
TOTP_DIGITS = 6
TOTP_PERIOD = 30


def derive_totp(
    secret_b32, now, period=TOTP_PERIOD, digits=TOTP_DIGITS, algorithm=TOTP_ALGORITHM
):
    """Compute the RFC 6238 TOTP code for a base32 shared secret.

    Fifteen lines of stdlib HMAC rather than a dependency, because RFC 6238 publishes test
    vectors: `test_ui_login.py` checks this against them, which is a stronger proof than
    "the library is popular".

    Args:
        secret_b32: the shared secret, base32, with or without `=` padding.
        now: Unix seconds to derive the code for. Passed in rather than read, so the test
          can pin it to the RFC's timestamps.

    Returns:
        The code as a zero-padded string of `digits` characters.
    """
    padded = secret_b32.strip().upper()
    padded += "=" * (-len(padded) % 8)
    key = base64.b32decode(padded)
    counter = struct.pack(">Q", int(now) // period)
    digest = hmac.new(key, counter, algorithm).digest()
    # Dynamic truncation: the low nibble of the last byte picks the 4-byte window, whose top
    # bit is masked off so the result never reads as negative.
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


# Authelia's own view of the caller's session. Cheaper than a service route and it needs no
# service to be up, so `--check` stays a question about the cookie rather than about whatever
# happened to be picked as a target.
STATE_ENDPOINT_PATH = "/api/state"

SECOND_FACTOR_PATH = "/api/secondfactor/totp"

# Deliberately NOT $CLAUDE_JOB_DIR/tmp: that is per-job and vanishes with the job, which
# would re-mint a session on every new one. ~/.claude survives across sessions and hosts
# the same kind of local operator state.
STATE_DIR = os.path.expanduser("~/.claude/playwright")
STATE_PATH = os.path.join(STATE_DIR, "authelia-state.json")

# The two_factor session lives in its OWN file and never upgrades the one above.
# `ui_mcp.sh` loads a jar unconditionally, so promoting the default would make every
# ordinary page load carry admin-capable auth — code-server is a shell as the repo user and
# longhorn deletes volumes and their B2 backup chain. Reaching those is opt-in per launch.
TWO_FACTOR_STATE_PATH = os.path.join(STATE_DIR, "authelia-state-2fa.json")

# A first-factor session asks for remember_me (1M), so it is worth saving. A two_factor one
# deliberately does not: `expiration: '1h'` and `inactivity: '5m'` are what keep an
# admin-capable cookie from lying around, and re-minting costs one typed code.
FIRST_FACTOR_LIFETIME = 29 * 24 * 3600
TWO_FACTOR_LIFETIME = 3600

TIMEOUT = 15


def state_path(two_factor=False):
    return TWO_FACTOR_STATE_PATH if two_factor else STATE_PATH


def portal_host(domain):
    """Authelia's LAN portal — the `authelia_url` of the `local.<domain>` cookie."""
    return f"auth.local.{domain}"


def cookie_domain(domain):
    """The cookie domain, with a leading dot.

    The dot issues the cookie for the whole `local.<domain>` cookie-domain, so one login covers
    every service route, not just the portal it was minted at.
    """
    return f".local.{domain}"


def build_storage_state(cookie_value, domain, expires):
    """The `storageState` JSON Playwright loads. Pure — the unit test drives this directly."""
    return {
        "cookies": [
            {
                "name": COOKIE_NAME,
                "value": cookie_value,
                "domain": cookie_domain(domain),
                "path": "/",
                "expires": expires,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def parse_set_cookie(header_text):
    """Pull the Authelia session value out of a raw HTTP response-header block.

    Returns None when the header is absent, which is how a rejected login presents —
    Authelia answers 200 with `{"status":"KO"}` and sets no cookie, so a status-code
    check alone would read a bad password as success.
    """
    for line in header_text.splitlines():
        if not line.lower().startswith("set-cookie:"):
            continue
        value = line.split(":", 1)[1].strip()
        name, _, rest = value.partition("=")
        if name.strip() == COOKIE_NAME:
            return rest.split(";", 1)[0]
    return None


def _curl(argv, stdin_text=None):
    return subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, timeout=TIMEOUT
    )


def post_json(host, path, body, cookie=None):
    """POST JSON to the portal and return its raw response headers.

    Bodies and cookies go in on stdin, never in argv — a password or a session value in
    argv is visible in `ps` and in any shell history that captured the call.
    """
    with tempfile.TemporaryDirectory() as tmp:
        headers_path = os.path.join(tmp, "headers")
        argv = [
            "curl",
            "-sS",
            "--max-time",
            str(TIMEOUT),
            "--resolve",
            f"{host}:443:{core.metallb_vip()}",
            "-D",
            headers_path,
            "-o",
            os.path.join(tmp, "body"),
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-H",
            f"Origin: https://{host}",
            "--data",
            "@-",
        ]
        stdin_text = body
        if cookie is not None:
            # curl reads its config from stdin too, so the body moves to a file to keep
            # both off argv.
            body_path = os.path.join(tmp, "post")
            # 0600 rather than the umask default: the TemporaryDirectory is already 0700,
            # so this is belt-and-braces on a file that holds a live TOTP code.
            fd = os.open(body_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(body)
            argv[argv.index("--data") + 1] = f"@{body_path}"
            argv += ["--config", "-"]
            stdin_text = f'cookie = "{COOKIE_NAME}={cookie}"\n'
        out = _curl(argv + [f"https://{host}{path}"], stdin_text=stdin_text)
        if out.returncode != 0:
            raise SystemExit(f"POST {path} failed: {out.stderr.strip()}")
        with open(headers_path) as f:
            return f.read()


def mint(two_factor=False, totp_code=None):
    """Log in and write the state file. Returns the path.

    A two_factor mint logs in as CLAUDE_USER, not as the operator, and upgrades that session
    through Authelia's second-factor endpoint. The code is derived from
    `authelia_claude_totp_secret` unless `totp_code` overrides it — see the module docstring
    for why that secret is readable here and what the dedicated identity buys.
    """
    two_factor = two_factor or totp_code is not None
    domain = core.sops_extract("domain")
    if two_factor:
        user = CLAUDE_USER
        password = core.sops_extract("authelia_claude_password")
        if totp_code is None:
            totp_code = derive_totp(
                core.sops_extract("authelia_claude_totp_secret"), time.time()
            )
    else:
        user = core.sops_extract("authelia_user")
        password = core.sops_extract("authelia_password")
    host = portal_host(domain)

    body = json.dumps(
        {
            "username": user,
            "password": password,
            # A two_factor session is deliberately short-lived; see TWO_FACTOR_LIFETIME.
            "keepMeLoggedIn": not two_factor,
            "targetURL": f"https://{host}/",
        }
    )

    cookie = parse_set_cookie(post_json(host, "/api/firstfactor", body))
    if cookie is None:
        raise SystemExit(
            "authelia issued no session cookie — check authelia_user / authelia_password "
            "in ansible/vars/secrets.yml (a wrong password still answers HTTP 200)"
        )

    if two_factor:
        upgraded = parse_set_cookie(
            post_json(
                host,
                SECOND_FACTOR_PATH,
                json.dumps({"token": totp_code, "targetURL": f"https://{host}/"}),
                cookie=cookie,
            )
        )
        # Authelia may raise the level on the existing session rather than reissue the
        # cookie, so no new Set-Cookie is normal and the first-factor value carries on.
        # Whether the upgrade actually took is settled by --check, not by this response.
        cookie = upgraded or cookie

    # Expire a little early so a stale jar surfaces as a re-mint rather than as a puzzling
    # mid-session redirect to the login portal.
    lifetime = TWO_FACTOR_LIFETIME if two_factor else FIRST_FACTOR_LIFETIME
    state = build_storage_state(cookie, domain, int(time.time()) + lifetime)

    path = state_path(two_factor)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    return path


def local_state_problem(state, now):
    """What is wrong with the state file on its own terms, or None if nothing is.

    A cheap pre-filter, NOT the verdict. It answers "is this file shaped like a live
    session", which is a strictly weaker question than "does Authelia still accept it" —
    see check().
    """
    cookies = (state or {}).get("cookies") or []
    if not cookies:
        return "holds no cookies"
    expires = cookies[0].get("expires", 0)
    remaining = expires - now
    if remaining <= 0:
        # Minutes, not days: a two_factor session lives an hour, so "expired 0d ago" would
        # be the usual reading and would tell nobody anything.
        return f"expired {int(-remaining / 60)}m ago"
    return None


def classify_state(payload, required_level=1):
    """(valid, detail) from Authelia's /api/state body.

    `authentication_level` is the field that matters: 0 is an anonymous session, 1 is
    one_factor, 2 is two_factor. Authelia answers 200 with level 0 to a cookie it no longer
    honours, so the status code says nothing on its own.

    `required_level` is 2 for the two_factor jar. A level-1 cookie is perfectly valid and
    still cannot open code-server, n8n or longhorn, so checking only for "authenticated"
    would call a jar good that is about to bounce off the portal.
    """
    if not isinstance(payload, dict):
        return False, "unreadable /api/state response"
    if payload.get("status") != "OK":
        return False, f"authelia reported status={payload.get('status')!r}"
    level = (payload.get("data") or {}).get("authentication_level")
    if not isinstance(level, int):
        return False, "no authentication_level in /api/state"
    if level < 1:
        return False, "cookie is no longer authenticated (authentication_level 0)"
    if level < required_level:
        return False, (
            f"session is only one_factor (authentication_level {level}); the two_factor "
            f"services need a fresh `--totp <code>`"
        )
    return True, f"authenticated (authentication_level {level})"


def check(two_factor=False):
    """Report whether the saved cookie is one Authelia still accepts.

    The expiry stamped in the file is a *claim*, not a fact, and the two come apart in the
    cases that matter: restarting Authelia or rotating `authelia_secret` invalidates every
    live session while the local timestamp goes on reading valid for weeks. Trusting it
    fails open — the browser launches with a dead cookie and lands on the login portal,
    which presents as a puzzling blank page rather than as an auth error.

    So the timestamp is only ever a fast reject; the verdict comes from asking Authelia.
    An unreachable portal counts as invalid, which is the fail-closed direction: minting
    needs the same network the browsing does, so there is nothing useful to do with a
    session that cannot be confirmed.
    """
    path = state_path(two_factor)
    mint_hint = "--totp <code>" if two_factor else "no arguments"
    if not os.path.exists(path):
        print(f"no state file at {path} — mint one by running with {mint_hint}")
        return 1
    with open(path) as f:
        state = json.load(f)

    problem = local_state_problem(state, time.time())
    if problem is not None:
        print(f"{path} {problem} — re-mint with {mint_hint}")
        return 1

    domain = core.sops_extract("domain")
    host = portal_host(domain)
    out = _curl(
        [
            "curl",
            "-sS",
            "--max-time",
            str(TIMEOUT),
            "--resolve",
            f"{host}:443:{core.metallb_vip()}",
            "--config",
            "-",
            f"https://{host}{STATE_ENDPOINT_PATH}",
        ],
        stdin_text=f'cookie = "{COOKIE_NAME}={state["cookies"][0]["value"]}"\n',
    )
    if out.returncode != 0:
        print(f"could not reach the Authelia portal: {out.stderr.strip()} — re-mint")
        return 1
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        payload = None

    valid, detail = classify_state(payload, required_level=2 if two_factor else 1)
    print(f"{detail} ({path})" if valid else f"{detail} — re-mint")
    return 0 if valid else 1


def classify_response(status, location):
    """Did the cookie actually reach the backend, or did Authelia intercept?

    Split out and pure because the distinction is the whole point of the check: a 302 to
    the portal is what an unauthenticated request looks like, and reading only the status
    code would score that as a reachable service.
    """
    if status == 302 and "auth.local." in (location or ""):
        return False, "redirected to the Authelia portal — cookie not accepted"
    if 200 <= status < 400:
        return True, f"reached the backend (HTTP {status})"
    return False, f"backend answered HTTP {status}"


def verify(service, two_factor=False):
    """Fetch one service route with the saved cookie and report whether it got through."""
    path = state_path(two_factor)
    if not os.path.exists(path):
        raise SystemExit(f"no state file at {path} — mint one first")
    with open(path) as f:
        state = json.load(f)
    cookie_value = state["cookies"][0]["value"]
    domain = core.sops_extract("domain")
    host = f"{service}.local.{domain}"

    # Cookie via stdin config, never argv — same reasoning as the login above.
    out = _curl(
        [
            "curl",
            "-sS",
            "-o",
            os.devnull,
            "--max-time",
            str(TIMEOUT),
            "--resolve",
            f"{host}:443:{core.metallb_vip()}",
            "-w",
            "%{http_code} %{redirect_url}",
            "--config",
            "-",
            f"https://{host}/",
        ],
        stdin_text=f'cookie = "{COOKIE_NAME}={cookie_value}"\n',
    )
    if out.returncode != 0:
        raise SystemExit(f"request to {service} failed: {out.stderr.strip()}")
    parts = out.stdout.split(None, 1)
    status = int(parts[0])
    location = parts[1] if len(parts) > 1 else ""
    ok, detail = classify_response(status, location)
    print(f"{service}: {detail}")
    return 0 if ok else 1


def main(argv=None):
    """Parse args and mint, check, verify, or locate an Authelia session state file.

    With no inspection flag, mints a new session — two_factor when `--two-factor` or
    `--totp` is given, else one_factor. `--path` only prints the state file location;
    `--check` and `--verify` inspect an existing session without minting a new one. Returns
    the exit code of the action taken; a bare mint always returns 0.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="ask Authelia whether the saved session is still honoured, without logging in",
    )
    parser.add_argument(
        "--path", action="store_true", help="print the state file path and exit"
    )
    parser.add_argument(
        "--verify",
        metavar="SERVICE",
        help="fetch <service>.local.<domain> with the saved cookie and report whether "
        "it reached the backend rather than the login portal",
    )
    parser.add_argument(
        "--totp",
        metavar="CODE",
        help="mint a two_factor session using this code instead of deriving one. "
        "Break-glass, for a seeded secret that has drifted from Authelia's own row",
    )
    parser.add_argument(
        "--two-factor",
        action="store_true",
        help="act on the two_factor state file — mint one for code-server / n8n / longhorn "
        "as the claude-ui user, or make --check / --verify / --path read that file",
    )
    args = parser.parse_args(argv)

    if args.path:
        print(state_path(args.two_factor))
        return 0
    if args.verify:
        return verify(args.verify, two_factor=args.two_factor)
    if args.check:
        return check(two_factor=args.two_factor)

    two_factor = args.two_factor or args.totp is not None
    path = mint(two_factor=two_factor, totp_code=args.totp)
    tier = "two_factor" if two_factor else "one_factor"
    user = CLAUDE_USER if two_factor else "the operator"
    print(f"minted a {tier} session as {user}; storage state written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
