"""The deployer's regex reader and the Ansible filter must agree on the live tree.

The filter decides the denylist; the deployer's reader only detects that a host's rendered
config has gone stale against it. If the two drift, that detection false-alarms and disarms
auto-deploy on a host that is actually converged — so pin them together against the real roles.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from deploy_logic import SHARED_K8S_ROLES, declared_denylist
from k8s_autodeploy import SHARED_ROLES, k8s_autodeploy_denylist
from _helpers import REPO as _REPO


_ANSIBLE = _REPO / "ansible"
_K8S_ROLES = _ANSIBLE / "roles/k8s"


def _sources() -> dict[str, str | None]:
    sources: dict[str, str | None] = {}
    for role in sorted(p for p in _K8S_ROLES.iterdir() if p.is_dir()):
        # Mirror the filter's own skips (k8s_autodeploy.py:99) — a stray dotdir or
        # __pycache__ under roles/k8s/ would otherwise be denied by the regex reader and
        # silently skipped by the filter, failing this test on an input that has nothing
        # to do with production.
        if role.name.startswith(".") or role.name == "__pycache__":
            continue
        defaults = role / "defaults/main.yml"
        sources[role.name] = defaults.read_text() if defaults.is_file() else None
    return sources


def test_the_shared_role_sets_match() -> None:
    assert SHARED_K8S_ROLES == SHARED_ROLES


def test_both_readers_derive_the_same_denylist() -> None:
    assert declared_denylist(_sources()) == frozenset(
        k8s_autodeploy_denylist(str(_ANSIBLE))
    )


# Adversarial and edge-case `defaults/main.yml` bodies, each paired with a label used to build a
# probe role name. New adversarial inputs belong in this list — it's where a future reader
# debugging a regex/filter drift will look first. `k8s_autodeploy_reason` is included wherever
# the entry is meant to reach a genuine filter-permitted outcome; the filter requires a reason on
# every declared role regardless of value, so an entry testing the true/false axis needs one to
# exercise that axis rather than tripping the (unrelated) missing-reason raise.
_CORPUS: list[tuple[str, str]] = [
    ("plain_true", 'k8s_autodeploy: true\nk8s_autodeploy_reason: "ok"\n'),
    ("plain_false", 'k8s_autodeploy: false\nk8s_autodeploy_reason: "ok"\n'),
    (
        "trailing_comment",
        "k8s_autodeploy: true  # noqa var-naming[no-role-prefix]\n"
        'k8s_autodeploy_reason: "ok"\n',
    ),
    (
        "duplicate_false_then_true",
        'k8s_autodeploy: false\nk8s_autodeploy: true\nk8s_autodeploy_reason: "ok"\n',
    ),
    (
        "duplicate_true_then_false",
        'k8s_autodeploy: true\nk8s_autodeploy: false\nk8s_autodeploy_reason: "ok"\n',
    ),
    ("no_space_after_colon", 'k8s_autodeploy:true\nk8s_autodeploy_reason: "ok"\n'),
    (
        # A decoy top-level-looking `true` sitting inside a multi-line quoted scalar, followed
        # by the real declaration. The regex can't tell YAML quoting from column-zero text, so
        # it sees two candidate matches and (correctly) denies on the disagreement.
        "decoy_in_multiline_scalar",
        'note: "line one\n'
        "k8s_autodeploy: true\n"
        'line three"\n'
        "k8s_autodeploy: false\n"
        'k8s_autodeploy_reason: "ok"\n',
    ),
    (
        "indented_key",
        'something:\n  k8s_autodeploy: true\n  k8s_autodeploy_reason: "ok"\n',
    ),
    ("crlf", 'k8s_autodeploy: true\r\nk8s_autodeploy_reason: "ok"\r\n'),
    ("empty_file", ""),
    ("no_declaration", "some_other_var: 1\n"),
    ("yaml_yes", 'k8s_autodeploy: yes\nk8s_autodeploy_reason: "ok"\n'),
    ("yaml_on", 'k8s_autodeploy: on\nk8s_autodeploy_reason: "ok"\n'),
    ("yaml_no", 'k8s_autodeploy: no\nk8s_autodeploy_reason: "ok"\n'),
    ("yaml_off", 'k8s_autodeploy: off\nk8s_autodeploy_reason: "ok"\n'),
    # A quoted "true" is a YAML string, not a boolean — the filter rejects it outright.
    ("quoted_string_true", 'k8s_autodeploy: "true"\nk8s_autodeploy_reason: "ok"\n'),
    # Bare non-boolean scalars. These are the entries that catch a widened _TRUE_VALUES: the
    # regex would start permitting them while the filter still rejects a non-bool. The quoted
    # case above does not catch it, because the quotes defeat the regex too.
    ("bare_word", 'k8s_autodeploy: maybe\nk8s_autodeploy_reason: "ok"\n'),
    ("bare_int", 'k8s_autodeploy: 1\nk8s_autodeploy_reason: "ok"\n'),
    ("bare_null", 'k8s_autodeploy: ~\nk8s_autodeploy_reason: "ok"\n'),
]


def _filter_permits(role: str, text: str) -> bool:
    """Whether the real Ansible filter, given its own isolated roles/k8s/ tree, calls `role`
    permitted — i.e. NOT present in the denylist it returns.

    The tree carries bare directories for the two SHARED_ROLES stand-ins (sufficient: the
    shared-role check only inspects defaults/main.yml when one exists) plus one always-denied
    anchor role, so evaluating a single corpus entry in isolation never trips the filter's own
    "derived an EMPTY denylist" guard. Any raise from the filter — a missing reason, a
    non-boolean value, invalid YAML, anything — counts as NOT permitted: a raise means the filter
    never actually vouched for the role.
    """
    with tempfile.TemporaryDirectory() as tmp:
        roles_dir = Path(tmp) / "roles" / "k8s"
        for shared in SHARED_ROLES:
            (roles_dir / shared).mkdir(parents=True)
        anchor = roles_dir / "zz-anchor" / "defaults"
        anchor.mkdir(parents=True)
        (anchor / "main.yml").write_text(
            "k8s_autodeploy: false\n"
            'k8s_autodeploy_reason: "keeps the corpus denylist non-empty"\n'
        )
        probe = roles_dir / role / "defaults"
        probe.mkdir(parents=True)
        # newline="": write the corpus text's line endings verbatim (the CRLF entry depends on
        # this — universal-newline translation would silently turn it back into LF on write).
        with open(probe / "main.yml", "w", newline="", encoding="utf-8") as fh:
            fh.write(text)
        try:
            denied = k8s_autodeploy_denylist(tmp)
        except Exception:
            return False
        return role not in denied


def test_the_regex_reader_never_permits_what_the_filter_denies() -> None:
    """The one-directional safety property this whole design depends on.

    If the regex reader calls a role permitted, the real YAML filter must call it permitted too
    — a regex that's looser than the filter is exactly the failure this test exists to catch,
    since it would make the deployer's staleness check agree with a config the filter would
    never have produced.

    The converse is fine and expected: the regex is deliberately stricter (unanimity across
    every match, no space-after-colon leniency, no CRLF tolerance), so filter-permits-while-
    regex-denies only ever causes a spurious disarm on a converged host, never a permitted
    deploy the filter would have refused.
    """
    for label, text in _CORPUS:
        role = f"probe_{label}"
        regex_permits = role not in declared_denylist({role: text})
        if regex_permits:
            assert _filter_permits(role, text), (
                f"{label}: the regex reader permitted a role the filter does not — this is "
                f"the exact drift this test exists to catch"
            )
