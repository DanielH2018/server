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
    uv run python scripts/diagnostics/ui_login.py --check    # report whether it is still valid
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

# Deliberately NOT $CLAUDE_JOB_DIR/tmp: that is per-job and vanishes with the job, which
# would re-mint a session on every new one. ~/.claude survives across sessions and hosts
# the same kind of local operator state.
STATE_PATH = os.path.expanduser("~/.claude/playwright/authelia-state.json")

TIMEOUT = 15


def portal_host(domain):
    """Authelia's LAN portal — the `authelia_url` of the `local.<domain>` cookie."""
    return f"auth.local.{domain}"


def cookie_domain(domain):
    """Leading dot: the cookie is issued for the whole `local.<domain>` cookie-domain, so
    one login covers every service route, not just the portal it was minted at."""
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


def mint():
    """Log in and write the state file. Returns the path."""
    domain = core.sops_extract("domain")
    user = core.sops_extract("authelia_user")
    password = core.sops_extract("authelia_password")
    host = portal_host(domain)

    body = json.dumps(
        {
            "username": user,
            "password": password,
            "keepMeLoggedIn": True,
            "targetURL": f"https://{host}/",
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        headers_path = os.path.join(tmp, "headers")
        # The password goes in on stdin, never in argv — otherwise it is visible in `ps`
        # and in any shell history that captured the call.
        out = _curl(
            [
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
                f"https://{host}/api/firstfactor",
            ],
            stdin_text=body,
        )
        if out.returncode != 0:
            raise SystemExit(f"authelia login request failed: {out.stderr.strip()}")
        with open(headers_path) as f:
            header_text = f.read()

    cookie = parse_set_cookie(header_text)
    if cookie is None:
        raise SystemExit(
            "authelia issued no session cookie — check authelia_user / authelia_password "
            "in ansible/vars/secrets.yml (a wrong password still answers HTTP 200)"
        )

    # remember_me is 1M; expire the file a day early so a stale jar surfaces as a re-mint
    # rather than as a puzzling mid-session redirect to the login portal.
    expires = int(time.time()) + 29 * 24 * 3600
    state = build_storage_state(cookie, domain, expires)

    os.makedirs(os.path.dirname(STATE_PATH), mode=0o700, exist_ok=True)
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    return STATE_PATH


def check():
    """Report whether the state file exists and its cookie is still in date."""
    if not os.path.exists(STATE_PATH):
        print(f"no state file at {STATE_PATH} — run without --check to mint one")
        return 1
    with open(STATE_PATH) as f:
        state = json.load(f)
    cookies = state.get("cookies") or []
    if not cookies:
        print(f"{STATE_PATH} holds no cookies — re-mint")
        return 1
    expires = cookies[0].get("expires", 0)
    remaining = expires - time.time()
    if remaining <= 0:
        print(f"session expired {int(-remaining / 86400)}d ago — re-mint")
        return 1
    print(f"session valid for {int(remaining / 86400)}d ({STATE_PATH})")
    return 0


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


def verify(service):
    """Fetch one service route with the saved cookie and report whether it got through."""
    if not os.path.exists(STATE_PATH):
        raise SystemExit(f"no state file at {STATE_PATH} — mint one first")
    with open(STATE_PATH) as f:
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
        "--check", action="store_true", help="report validity without logging in"
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
    args = parser.parse_args(argv)

    if args.path:
        print(STATE_PATH)
        return 0
    if args.verify:
        return verify(args.verify)
    if args.check:
        return check()

    path = mint()
    print(f"logged in as the authelia user; storage state written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
