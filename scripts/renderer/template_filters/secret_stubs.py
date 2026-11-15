"""The secret stub form of the shadow render (target slice R1).

The shadow render compares `homelab-render` with the Ansible manifests role for identical
inputs, and the inputs it can share without decrypting anything are stub values: every
secret key `K` renders as `stub-` plus `K` with underscores turned to hyphens. That shape is a
valid DNS label, so a stubbed host name still passes `kubectl apply --dry-run=server`. This
module is the one definition of the form, which the renderer and its render guards are
built to share, so neither restates it. `docs/homelab-render.md` is the operator's page.
"""

from collections.abc import Mapping

STUB_PREFIX = "stub-"


def homelab_secret_stub(key) -> str:
    """The stub value of the secret variable `key`: `stub-<key with hyphens>`."""
    return STUB_PREFIX + str(key).replace("_", "-")


def homelab_stub_keys(secrets) -> list[str]:
    """The sorted secret key names that take a stub, from the parsed `secrets.yml` mapping.

    The mapping may be the ciphertext file: only its keys are read. `sops`, SOPS's own
    metadata block, is left out.
    """
    return sorted(str(key) for key in secrets if key != "sops")


def homelab_unstubbed_keys(keys, values) -> list[str]:
    """The names in `keys` whose value is not exactly its `homelab_secret_stub`.

    `values` is either a mapping of name to value, where a missing name counts as unstubbed,
    or a sequence parallel to `keys`. The result holds names only, never a value, so an
    assert may print it whatever the run loaded.

    Raises:
        ValueError: `values` is a sequence of a different length from `keys`.
    """
    keys = [str(key) for key in keys]
    if isinstance(values, Mapping):
        pairs = [(key, values.get(key)) for key in keys]
    else:
        values = list(values)
        if len(values) != len(keys):
            raise ValueError(
                f"homelab_unstubbed_keys: {len(keys)} keys but {len(values)} values"
            )
        pairs = list(zip(keys, values, strict=True))
    return [
        key
        for key, value in pairs
        if not (isinstance(value, str) and value == homelab_secret_stub(key))
    ]
