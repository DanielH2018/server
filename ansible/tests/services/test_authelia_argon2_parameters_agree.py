"""Authelia's argon2 parameters are written twice, and nothing else holds the copies together.

`roles/k8s/authelia/templates/config-secret.yaml.j2` renders the parameters the running portal
uses to MINT a digest (its own password reset). `roles/k8s/authelia/tasks/main.yml` mints the
digests that actually land in the users database, in a throwaway pod, with the same numbers
spelled as CLI flags. Drift between them is invisible by construction: Authelia reads the
algorithm and parameters back off each stored digest's own `$argon2id$` PHC prefix
(`crypt.Decode`, `file_user_provider_database.go` at v4.39.21), so a login succeeds whatever
the config block says and a deploy is green either way. Under the legacy flat spelling the two
copies stood 1024x apart on `memory` for as long as the block existed (#1621); #1643 made them
agree, and this guard is what keeps them agreeing.

The flag-to-key mapping is NOT identity — `--salt-size` is `salt_length` and `--key-size` is
`key_length` — which is itself the reason it is pinned here rather than left to a reader.

Each rule is a predicate with a passing and a rejecting input, then applied to the real
rendered manifest and the real task file behind non-vacuity assertions.
"""

import re

import pytest
from _helpers import ROLES
from _k8s_render import rendered_docs
from lib import yaml_fast

TASKS = ROLES / "k8s/authelia/tasks/main.yml"

# The CLI flag each config key is spelled as. Pinned, because the mapping is not identity and
# a reader who assumed it is would accept `--salt-size 32` against `key_length: 32`.
FLAG_TO_CONFIG_KEY = {
    "--iterations": "iterations",
    "--memory": "memory",
    "--parallelism": "parallelism",
    "--salt-size": "salt_length",
    "--key-size": "key_length",
}

# `--password` carries the input, not a parameter, so it has no config counterpart. Every OTHER
# flag on the generate command is a parameter this guard must compare — including a `--variant`
# nobody has added yet. Rather than enumerate CLI defaults that cannot be read off the tree,
# the flag set is held to exactly the table above and an unrecognised flag fails naming itself.
IGNORED_FLAGS = frozenset({"--password"})

# The one-shot pods whose commands must be found. A count would fail saying a number moved; a
# name says which task went missing, and an empty parse can no longer read as agreement.
HASH_TASK_PODS = frozenset({"authelia-hash", "authelia-hash-claude"})

# There is no `--variant` flag on either command, so there is nothing to compare it against.
# It is pinned to a literal instead: `argon2id`, the long spelling the published v4.39 schema's
# enum carries and the value the template's own comment settles on.
EXPECTED_VARIANT = "argon2id"

_MARKER = "crypto hash generate argon2"
_FLAG = re.compile(r"(--[a-z][a-z0-9-]*)(?:[=\s]+)([^\s'\"]+)")


def argon2_flags(cmd: str) -> dict[str, str]:
    """The argon2 flags on a `crypto hash generate argon2` command, as {flag: value}.

    Only the segment after the subcommand is read, so `kubectl run`'s own `--image`/`--quiet`
    flags never reach the comparison.
    """
    if _MARKER not in cmd:
        return {}
    tail = cmd.split(_MARKER, 1)[1]
    return {
        flag: value for flag, value in _FLAG.findall(tail) if flag not in IGNORED_FLAGS
    }


def unexpected_flags(flags: dict[str, str]) -> set[str]:
    """Flag names with no entry in the pinned table — a parameter nothing compares."""
    return set(flags) - set(FLAG_TO_CONFIG_KEY)


def mismatches(config_argon2: dict, flags: dict[str, str]) -> list[str]:
    """The parameters the config block and the CLI flags disagree on, or that one side omits."""
    out = []
    for flag, key in FLAG_TO_CONFIG_KEY.items():
        if flag not in flags:
            out.append(
                f"{flag} missing from the command ({key}={config_argon2.get(key)!r})"
            )
        elif key not in config_argon2:
            out.append(f"{key} missing from the config block ({flag}={flags[flag]!r})")
        elif str(config_argon2[key]) != flags[flag]:
            out.append(f"{key}={config_argon2[key]!r} but {flag}={flags[flag]!r}")
    return out


# --- the predicates, each with a passing and a rejecting input -----------------------

GOOD_CONFIG = {
    "variant": "argon2id",
    "iterations": 3,
    "salt_length": 16,
    "parallelism": 4,
    "memory": 65536,
    "key_length": 32,
}
GOOD_CMD = (
    "k3s kubectl -n homelab run authelia-hash --rm -i --attach --quiet "
    "--restart=Never --image=authelia:4.39.21 --command -- "
    'sh -c \'IFS= read -r pw; authelia crypto hash generate argon2 --password "$pw" '
    "--iterations 3 --memory 65536 --parallelism 4 --salt-size 16 --key-size 32'"
)


def test_agreeing_parameters_are_clean():
    assert mismatches(GOOD_CONFIG, argon2_flags(GOOD_CMD)) == []


def test_a_changed_flag_is_flagged():
    """The #1621 shape: one side moved, the other did not, and every gate stays green."""
    mutated = GOOD_CMD.replace("--memory 65536", "--memory 64")
    assert mismatches(GOOD_CONFIG, argon2_flags(mutated))


def test_a_changed_config_value_is_flagged():
    """The same drift from the other direction."""
    assert mismatches({**GOOD_CONFIG, "iterations": 4}, argon2_flags(GOOD_CMD))


def test_the_non_identity_mapping_is_not_crossed():
    """`--salt-size` is `salt_length`; a reader who assumed identity would accept this."""
    crossed = GOOD_CMD.replace("--salt-size 16", "--salt-size 32")
    assert mismatches(GOOD_CONFIG, argon2_flags(crossed))


def test_kubectl_run_flags_are_not_parsed_as_parameters():
    """`--image` and `--quiet` sit before the subcommand and must not reach the table."""
    assert unexpected_flags(argon2_flags(GOOD_CMD)) == set()


def test_an_unrecognised_parameter_flag_is_flagged():
    """A `--variant` (or anything else) added to the command has nothing comparing it."""
    extended = GOOD_CMD.replace("--iterations 3", "--variant argon2i --iterations 3")
    assert unexpected_flags(argon2_flags(extended)) == {"--variant"}


# --- applied to the real rendered manifest and the real task file --------------------


@pytest.fixture(scope="module")
def rendered_argon2():
    """The `argon2` mapping out of the rendered `authentication_backend.file.password` block."""
    for role, _tpl, doc in rendered_docs():
        if role != "authelia" or doc.get("kind") != "Secret":
            continue
        if (doc.get("metadata") or {}).get("name") != "authelia-config":
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        config = yaml_fast.safe_load(raw or "") or {}
        password = ((config.get("authentication_backend") or {}).get("file") or {}).get(
            "password"
        ) or {}
        assert password.get("algorithm") == "argon2", (
            "the rendered password block does not name the argon2 algorithm, so the mapping "
            f"below would not be the one that mints digests; got {password.get('algorithm')!r}"
        )
        argon2 = password.get("argon2") or {}
        assert set(argon2) >= set(FLAG_TO_CONFIG_KEY.values()), (
            "the rendered argon2 mapping is missing parameters this guard compares, so the "
            "comparison would be vacuous: "
            f"{sorted(set(FLAG_TO_CONFIG_KEY.values()) - set(argon2))}"
        )
        return argon2
    pytest.fail("no rendered authelia-config Secret")


@pytest.fixture(scope="module")
def hash_commands():
    """{pod name: cmd} for every hash-generation task in the authelia role."""
    tasks = yaml_fast.safe_load(TASKS.read_text()) or []
    found = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        cmd = (task.get("ansible.builtin.command") or {}).get("cmd", "")
        if not isinstance(cmd, str) or _MARKER not in cmd:
            continue
        pod = re.search(r"\brun\s+(\S+)", cmd)
        found[pod.group(1) if pod else task.get("name", "?")] = cmd
    assert set(found) >= HASH_TASK_PODS, (
        "a hash-generation task this guard compares is gone from the authelia role, so its "
        f"flags are no longer checked against the config: {sorted(HASH_TASK_PODS - set(found))}"
    )
    return found


def test_the_rendered_variant_is_the_long_spelling(rendered_argon2):
    assert rendered_argon2.get("variant") == EXPECTED_VARIANT, (
        f"argon2.variant must be {EXPECTED_VARIANT!r} — no CLI flag pins it, and the published "
        f"v4.39 schema's enum carries the long spellings; got {rendered_argon2.get('variant')!r}"
    )


def test_every_hash_command_names_only_compared_parameters(hash_commands):
    for pod, cmd in sorted(hash_commands.items()):
        extra = unexpected_flags(argon2_flags(cmd))
        assert extra == set(), (
            f"{pod} passes {sorted(extra)}, which nothing in the config block is compared "
            f"against. Add it to FLAG_TO_CONFIG_KEY with its config key"
        )


def test_every_hash_command_agrees_with_the_rendered_config(
    rendered_argon2, hash_commands
):
    for pod, cmd in sorted(hash_commands.items()):
        bad = mismatches(rendered_argon2, argon2_flags(cmd))
        assert bad == [], (
            f"{pod}'s argon2 flags disagree with the rendered config block: {bad}. Verification "
            f"reads parameters off each digest's own PHC prefix, so nothing else would report it"
        )


def test_a_mutated_real_command_is_flagged(rendered_argon2, hash_commands):
    """Red proof on the real data path, not a synthetic dict."""
    pod = sorted(hash_commands)[0]
    mutated = re.sub(r"--memory\s+\d+", "--memory 64", hash_commands[pod])
    assert mutated != hash_commands[pod], (
        f"{pod}'s command carries no --memory flag to mutate"
    )
    assert mismatches(rendered_argon2, argon2_flags(mutated)), (
        "the comparison passed a command whose --memory is 1024x off, which is the exact "
        "#1621 defect — the guard is not reading one of the two sides"
    )
