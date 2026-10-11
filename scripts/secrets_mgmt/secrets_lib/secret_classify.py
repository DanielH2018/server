"""Which rotation tier a secret's NAME puts it in.

Pure name matching, no I/O and no imports outside the standard library: `secret_registry.sync()`
classifies each newly registered secret with this, and an operator overrides the verdict by
editing that secret's `tier` in `ansible/secret_rotation.yml` (`sync` preserves an override).

The tiers themselves, and the cadence each carries, are documented in
`scripts/secrets_mgmt/secret_rotation.py`'s module docstring and published as
`docs/reference/secrets.md`.
"""

# Classification by name suffix only. First matching rule wins; default is `assisted` (the safe,
# reminds-but-doesn't-touch tier). An `external` or `pinned` key is not recognisable by name, so
# it gets its tier by a registry edit after its first `sync`; a list of such names here only
# drifted from the registry, which is the source of truth once a key is registered (#3749).
_IGNORE_SUFFIX = ("_user", "_username")


def classify(name: str) -> str:
    """The rotation tier `sync` gives a new secret by name: ignore, auto, or assisted."""
    if name.endswith(_IGNORE_SUFFIX):
        return "ignore"
    if name.endswith("_push_token"):
        return "auto"
    return "assisted"
