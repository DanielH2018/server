"""The census rows over `prek.toml` and `.gitleaks.toml`."""

import re
import tomllib

from _row_table import Census, Subject, tracked


# ── prek.toml ─────────────────────────────────────────────────────────────────────────

_PREK = "prek.toml"
_COMMIT_ONLY = ["pre-commit"]
# One of the three hooks whose upstream manifest declares pre-push. A census that stops finding
# it has stopped reading the repo the bug lives in.
_NAMED_REMOTE_HOOK = "trailing-whitespace"
_MIN_UV_ENTRIES = 15


def _prek_hooks(text: str, *, remote: bool) -> list[dict]:
    return [
        hook
        for repo in tomllib.loads(text).get("repos", [])
        if (repo.get("repo") not in ("local", "meta")) == remote
        for hook in repo.get("hooks", [])
    ]


def _default_stages_offence(subject: Subject) -> list[str]:
    stages = tomllib.loads(subject.text).get("default_stages")
    return [] if stages == _COMMIT_ONLY else [f"default_stages is {stages!r}"]


def _remote_hook_offence(subject: Subject) -> list[str]:
    hooks = _prek_hooks(subject.text, remote=True)
    problems = [
        f"remote hook {hook.get('id', '?')!r} does not pin stages to pre-commit"
        for hook in hooks
        if hook.get("stages") != _COMMIT_ONLY
    ]
    if _NAMED_REMOTE_HOOK not in [hook.get("id") for hook in hooks]:
        problems.append(
            f"{_NAMED_REMOTE_HOOK} is no longer a remote hook; the census reads the wrong repos"
        )
    return problems


def _uv_run_hooks(text: str) -> list[tuple[str, str]]:
    return [
        (hook.get("id", "?"), hook["entry"])
        for hook in _prek_hooks(text, remote=False)
        if str(hook.get("entry", "")).startswith("uv run")
    ]


def _unfrozen_offence(subject: Subject) -> list[str]:
    return [
        f"hook {hook_id!r} runs `{entry}` without --frozen"
        for hook_id, entry in _uv_run_hooks(subject.text)
        if not entry.startswith("uv run --frozen ")
    ]


def _local_hook(entry: str) -> str:
    return f'[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "a"\nentry = "{entry}"\n'


_REMOTE_REPO = '[[repos]]\nrepo = "https://example.invalid/hooks"\nrev = "v1"\n'
_PINNED_REMOTE = f'{_REMOTE_REPO}[[repos.hooks]]\nid = "trailing-whitespace"\nstages = ["pre-commit"]\n'

PREK_DEFAULTS_TO_THE_COMMIT_STAGE = Census(
    name="prek-defaults-every-hook-to-the-commit-stage",
    reason=(
        "A hook without `stages` runs at EVERY stage, so a `.git/hooks/pre-push` shim an older "
        "`prek install` left behind re-ran every commit hook on push, fixers included. "
        '`default_stages = ["pre-commit"]` in prek.toml closes that for hooks defined here.'
    ),
    files=lambda: tracked(_PREK),
    offence=_default_stages_offence,
    red=(
        Subject(_PREK, '[[repos]]\nrepo = "local"\n'),
        Subject(_PREK, 'default_stages = ["pre-commit", "pre-push"]\n'),
    ),
    green=(Subject(_PREK, 'default_stages = ["pre-commit"]\n'),),
    must_find=frozenset({_PREK}),
)

PREK_REMOTE_HOOKS_PIN_THE_COMMIT_STAGE = Census(
    name="every-remote-prek-hook-pins-the-commit-stage",
    reason=(
        "`default_stages` does not reach a hook whose upstream manifest declares `stages` "
        "itself: pre-commit-hooks v6.0.0 lists `[pre-commit, pre-push, manual]` on "
        "trailing-whitespace, end-of-file-fixer and check-added-large-files, and those three "
        "still ran at pre-push with `default_stages` set. So a hook from a remote repo states "
        "its own."
    ),
    files=lambda: tracked(_PREK),
    offence=_remote_hook_offence,
    red=(
        Subject(
            _PREK,
            f'{_PINNED_REMOTE}[[repos.hooks]]\nid = "a"\n'
            '[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "c"\n',
        ),
        Subject(
            _PREK,
            f'{_REMOTE_REPO}[[repos.hooks]]\nid = "b"\nstages = ["pre-commit"]\n',
        ),
    ),
    green=(
        Subject(
            _PREK,
            f'{_PINNED_REMOTE}[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "c"\n',
        ),
    ),
    must_find=frozenset({_PREK}),
)

PREK_UV_ENTRIES_ARE_FROZEN = Census(
    name="every-prek-uv-run-hook-is-frozen",
    reason=(
        "The unattended production paths (`homelab-checkout-sync`'s venv sync, "
        "`secret-rotation-audit`) run uv with `--frozen`, which installs the committed "
        "`uv.lock` as-is. A bare `uv run` in a hook re-resolves instead, so the gate could "
        "pass against an environment the primary checkout never builds. `uv lock --check` "
        "catches the lock drifting; this keeps the hooks resolving from the same lock "
        "checkout-sync does, so the two cannot disagree on which environment a gate ran in."
    ),
    files=lambda: tracked(_PREK),
    offence=_unfrozen_offence,
    count=lambda subject: len(_uv_run_hooks(subject.text)),
    min_matches=_MIN_UV_ENTRIES,
    red=(Subject(_PREK, _local_hook("uv run python x.py")),),
    green=(Subject(_PREK, _local_hook("uv run --frozen python x.py")),),
    must_find=frozenset({_PREK}),
)


# ── .gitleaks.toml ────────────────────────────────────────────────────────────────────

_GITLEAKS = ".gitleaks.toml"
_DIGEST = "2678c3451a396ec559a9e8463808ec85c1e6778350f4a4515f1fdfa02d3a83b0"
_RULE = ["generic-api-key"]


def _allowlist_problems(
    entries: list[dict],
    what: str,
    want: dict[str, object],
    admitted: tuple[str, ...],
    refused: tuple[str, ...],
) -> list[str]:
    if len(entries) != 1:
        return [f"{len(entries)} allowlists select {what}, want exactly one"]
    [entry] = entries
    problems = [
        f"{key} is {entry.get(key)!r}, want {value!r}"
        for key, value in want.items()
        if entry.get(key) != value
    ]

    def admits(line: str) -> bool:
        return any(re.search(regex, line) for regex in entry.get("regexes", []))

    problems += [
        f"does not admit {line.strip()!r}" for line in admitted if not admits(line)
    ]
    problems += [f"admits {line.strip()!r}" for line in refused if admits(line)]
    return problems


def _manifest_digest_offence(subject: Subject) -> list[str]:
    entries = [
        entry
        for entry in tomllib.loads(subject.text)["allowlists"]
        if entry.get("paths") == [r"^usr/lib/homelab/manifest\.json$"]
    ]
    return _allowlist_problems(
        entries,
        "the host package manifest",
        {"condition": "AND", "targetRules": _RULE, "regexTarget": "line"},
        (
            f' "secrets_inputs_sha256": "{_DIGEST}",',  # gitleaks:allow
            f' "secrets_inputs_sha256": "{_DIGEST}"',  # gitleaks:allow
        ),
        (
            f' "api_token_sha256": "{_DIGEST}",',  # gitleaks:allow
            f' "secrets_inputs_sha256": "{_DIGEST}0",',  # gitleaks:allow
            ' "secrets_inputs_sha256": "K005aBcDeFgHiJkLmNoPqRsTuVwXyZ01",',  # gitleaks:allow
            f' "secrets_inputs_sha256": "{_DIGEST}", "token": "x"',  # gitleaks:allow
        ),
    )


def _secret_key_name_offence(subject: Subject) -> list[str]:
    entries = [
        entry
        for entry in tomllib.loads(subject.text)["allowlists"]
        if entry.get("regexTarget") == "line" and entry["paths"][0].startswith("^out/")
    ]
    return _allowlist_problems(
        entries,
        "the rendered bundle",
        {
            "condition": "AND",
            "targetRules": _RULE,
            "paths": [r"^out/cluster/[^/]+/[^/]+\.yaml$"],
        },
        (
            "          - key: b2_probe_application_key",  # gitleaks:allow
            "          - key: ha_token",
            "              key: kuma_status_page_sync_push_token",
        ),
        (
            "          - key: 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c",  # gitleaks:allow
            "          - key: K005aBcDeFgHiJkLmNoPqRsTuVwXyZ01",
            "          - key: abcdefghijklmnopqrstuvwxyz012345",
            "          - key: b2_probe_application_key K005aBcDeFgHiJkLmNoPqRs",  # gitleaks:allow
            "          value: b2_probe_application_key",
        ),
    )


def _allowlist_toml(paths: str, regex: str, *, condition: str = "AND") -> str:
    return (
        "[[allowlists]]\n"
        f'condition = "{condition}"\n'
        'targetRules = ["generic-api-key"]\n'
        f"paths = ['''{paths}''']\n"
        'regexTarget = "line"\n'
        f"regexes = ['''{regex}''']\n"
    )


_MANIFEST_PATH = r"^usr/lib/homelab/manifest\.json$"
_MANIFEST_REGEX = r'^\s*"secrets_inputs_sha256": "[0-9a-f]{64}",?\s*$'
_BUNDLE_PATH = r"^out/cluster/[^/]+/[^/]+\.yaml$"
_KEY_REGEX = r"^\s*-?\s*key: [a-z][a-z0-9]*(?:_[a-z0-9]+)+\s*$"

GITLEAKS_MANIFEST_DIGEST_ALLOWLIST = Census(
    name="gitleaks-manifest-digest-allowlist-stays-narrow",
    reason=(
        "The `host_packages` job scans each named package root with gitleaks, and "
        '`generic-api-key` reads the manifest\'s `"secrets_inputs_sha256": "<64 hex>"` line as '
        "a credential. The allowlist admits that one line shape in "
        "`usr/lib/homelab/manifest.json` for that one rule, and needs both the path and the "
        "line. Another key with a 64-hex value, or a longer value on the same key, still "
        "trips."
    ),
    files=lambda: tracked(_GITLEAKS),
    offence=_manifest_digest_offence,
    red=(
        Subject(_GITLEAKS, _allowlist_toml(_MANIFEST_PATH, r"^.*$")),
        Subject(_GITLEAKS, _allowlist_toml(_MANIFEST_PATH, r'"[0-9a-f]{64}"')),
        Subject(_GITLEAKS, _allowlist_toml(_MANIFEST_PATH, r"secrets_inputs_sha256")),
        Subject(
            _GITLEAKS,
            _allowlist_toml(_MANIFEST_PATH, _MANIFEST_REGEX, condition="OR"),
        ),
        Subject(_GITLEAKS, "[[allowlists]]\ndescription = 'unrelated'\n"),
    ),
    green=(
        Subject(_GITLEAKS, _allowlist_toml(_MANIFEST_PATH, _MANIFEST_REGEX)),
        Subject(_GITLEAKS),
    ),
    must_find=frozenset({_GITLEAKS}),
)

GITLEAKS_SECRET_KEY_NAME_ALLOWLIST = Census(
    name="gitleaks-secret-key-name-allowlist-stays-narrow",
    reason=(
        "promote's render job scans the publish bundle with gitleaks, and `generic-api-key` "
        "reads a workload's `key: <secret key name>` line as a credential (the B2 probe's "
        "Secret item name tripped it). The allowlist admits that line shape and nothing "
        "wider: one rule, the bundle's manifests only, and a snake_case name with at least "
        "one underscore. A credential on the same line has no underscore, so it still trips."
    ),
    files=lambda: tracked(_GITLEAKS),
    offence=_secret_key_name_offence,
    red=(
        Subject(_GITLEAKS, _allowlist_toml(_BUNDLE_PATH, r"^\s*-?\s*key: .*$")),
        Subject(
            _GITLEAKS, _allowlist_toml(_BUNDLE_PATH, r"^\s*-?\s*key: [a-z0-9]+\s*$")
        ),
        Subject(_GITLEAKS, _allowlist_toml(_BUNDLE_PATH, _KEY_REGEX, condition="OR")),
        Subject(_GITLEAKS, _allowlist_toml(r"^out/.*$", _KEY_REGEX)),
        Subject(_GITLEAKS, "[[allowlists]]\ndescription = 'unrelated'\n"),
    ),
    green=(
        Subject(_GITLEAKS, _allowlist_toml(_BUNDLE_PATH, _KEY_REGEX)),
        Subject(_GITLEAKS),
    ),
    must_find=frozenset({_GITLEAKS}),
)
