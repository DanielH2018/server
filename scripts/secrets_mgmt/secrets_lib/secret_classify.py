"""Which rotation tier a secret's NAME puts it in.

Pure name matching, no I/O and no imports outside the standard library: `secret_registry.sync()`
classifies each newly registered secret with this, and an operator overrides the verdict by
editing that secret's `tier` in `secrets/secret_rotation.yml` (`sync` preserves an override).

The tiers themselves, and the cadence each carries, are documented in
`scripts/secrets_mgmt/secret_rotation.py`'s module docstring and published as
`docs/reference/secrets.md`.
"""

# An `external` or `pinned` key is not recognisable by name, so its tier comes from a registry edit
# after its first `sync`.
_IGNORE_SUFFIX = ("_user", "_username")


def classify(name: str) -> str:
    """The rotation tier `sync` gives a new secret by name: ignore or assisted."""
    if name.endswith(_IGNORE_SUFFIX):
        return "ignore"
    return "assisted"
