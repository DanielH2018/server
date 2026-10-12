"""Ansible filter plugin holding the secret stub form of the shadow render (target slice R1).

The shadow render compares `homelab-render` with the Ansible manifests role for identical
inputs, and the inputs it can share without decrypting anything are stub values: every
secret key `K` renders as `stub-` plus `K` with underscores turned to hyphens. That shape is a
valid DNS label, so a stubbed host name still passes `kubectl apply --dry-run=server`. This
module is the one definition of the form, which R1's renderer and its playbook checks are
built to share, so neither restates it. `docs/homelab-render.md` is the operator's page.

`google_assistant_service_account` is the one key a template parses: home-assistant's
`templates/config/secrets.yaml.j2` runs it through `from_json` and reads three fields. Its
stub is therefore a JSON object holding exactly those fields, serialised with sorted keys so
it is one fixed string.

Ansible loads every module under `filter_plugins/` in every play, so this one imports the
standard library only. `FilterModule.filters()` returns one literal dict, because
`scripts/deploy_tools/narrow_lib/filters.py:filter_names` reads the filter names statically
and refuses any other shape.
"""

import json
from collections.abc import Mapping

STUB_PREFIX = "stub-"

# Variables that never take a stub, each with its reason. They stay out of the stub key list,
# so neither the stub file nor the stub content check names them.
NEVER_STUB = {
    "become_password": (
        "ansible/pre_tasks/load_secrets.yml hands it to privilege escalation, and no "
        "template reads it"
    ),
}

# Keys a template parses, mapped to the fields it reads. Target slice R2 removes the parse.
_STRUCTURED = {
    "google_assistant_service_account": ("project_id", "client_email", "private_key"),
}


def _label(name: str) -> str:
    return STUB_PREFIX + name.replace("_", "-")


def homelab_secret_stub(key) -> str:
    """The stub value of the secret variable `key`.

    `stub-<key with hyphens>` for every key but a structured one, whose stub is a sorted-key
    JSON object string with one `stub-<key>-<field>` value per field its template reads.
    """
    key = str(key)
    fields = _STRUCTURED.get(key)
    if fields is None:
        return _label(key)
    return json.dumps(
        {field: f"{_label(key)}-{field.replace('_', '-')}" for field in fields},
        sort_keys=True,
    )


def homelab_stub_keys(secrets) -> list[str]:
    """The sorted secret key names that take a stub, from the parsed `secrets.yml` mapping.

    The mapping may be the ciphertext file: only its keys are read. `sops`, SOPS's own
    metadata block, and the names in `NEVER_STUB` are left out.
    """
    return sorted(
        str(key) for key in secrets if key != "sops" and str(key) not in NEVER_STUB
    )


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


class FilterModule:
    # Unwrapped by `_as_filter`, unlike the siblings: importing `filter_errors` would add a
    # sibling import to a plugin that must load in every play, and the one ValueError raised
    # here already names its filter.
    def filters(self):
        return {
            "homelab_secret_stub": homelab_secret_stub,
            "homelab_stub_keys": homelab_stub_keys,
            "homelab_unstubbed_keys": homelab_unstubbed_keys,
        }
