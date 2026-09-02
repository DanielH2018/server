#!/usr/bin/env python3
"""Mint a Playwright storage-state file holding a logged-in Authelia session.

A browser driven on this host cannot reach the homelab UIs the way a laptop does,
for two reasons this script exists to solve:

  1. Every `*.local.<domain>` route sits behind Authelia's `one_factor` policy
     (`roles/k8s/authelia/templates/config-secret.yaml.j2:100`), so an unauthenticated
     browser sees the login portal instead of the app.
  2. This host's resolver bypasses the LAN DNS, so `.local.<domain>` does not resolve
     to the cluster edge from a shell here. Same trap `probe_core.k8s_endpoint` documents;
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
    uv run python scripts/diagnostics/ui_login.py            # mint (or refresh) the state file
    uv run python scripts/diagnostics/ui_login.py --check    # ask Authelia if the session still stands
    uv run python scripts/diagnostics/ui_login.py --totp 123456   # a two_factor session, ~1h

The `--totp` form exists because code-server, n8n and longhorn are `two_factor`
(`roles/k8s/authelia/templates/config-secret.yaml.j2:85-99`), and a one_factor cookie
bounces off them. The code is typed, never derived: the TOTP shared secret stays on the
phone. Storing it in SOPS would put both factors under one age key, and rotating it would
mean re-enrolling the device — the one credential here whose rotation costs more than an
edit. Nothing runs these unattended anyway, since the `ui` test marker is deselected in CI.
    uv run python scripts/diagnostics/ui_login.py --path     # print the state file path

Exit 0 = a usable state file is on disk. Exit 1 = it is missing, expired or rejected.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import probe_core as core

# The cookie Authelia issues for the LAN cookie-domain. Mirrors
# `authelia_k8s_cookie_name` in roles/k8s/authelia/defaults/main.yml:48 — a rename there
# must land here too, which test_ui_login.py asserts against the role default.
COOKIE_NAME = "authelia_session_k8s"

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
    """Leading dot:

    the cookie is issued for the whole `local.<domain>` cookie-domain, so one login covers every
    service route, not just the portal it was minted at.
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
            with open(body_path, "w") as f:
                f.write(body)
            argv[argv.index("--data") + 1] = f"@{body_path}"
            argv += ["--config", "-"]
            stdin_text = f'cookie = "{COOKIE_NAME}={cookie}"\n'
        out = _curl(argv + [f"https://{host}{path}"], stdin_text=stdin_text)
        if out.returncode != 0:
            raise SystemExit(f"POST {path} failed: {out.stderr.strip()}")
        with open(headers_path) as f:
            return f.read()


def mint(totp_code=None):
    """Log in and write the state file. Returns the path.

    With `totp_code`, the first-factor session is upgraded through Authelia's second-factor
    endpoint and written to the two_factor state file instead. The code is passed in rather
    than derived: keeping the TOTP shared secret OFF this host is the point — storing it
    beside the password in SOPS would put both factors under one age key, and rotating it
    would mean re-enrolling the phone.
    """
    two_factor = totp_code is not None
    domain = core.sops_extract("domain")
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
        help="mint a two_factor session using this code from your authenticator, for "
        "code-server / n8n / longhorn. Written to a separate, short-lived state file; the "
        "TOTP secret itself is never stored here",
    )
    parser.add_argument(
        "--two-factor",
        action="store_true",
        help="make --check / --verify / --path act on the two_factor state file",
    )
    args = parser.parse_args(argv)

    if args.path:
        print(state_path(args.two_factor))
        return 0
    if args.verify:
        return verify(args.verify, two_factor=args.two_factor)
    if args.check:
        return check(two_factor=args.two_factor)

    path = mint(totp_code=args.totp)
    tier = "two_factor" if args.totp else "one_factor"
    print(
        f"minted a {tier} session as the authelia user; storage state written to {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
