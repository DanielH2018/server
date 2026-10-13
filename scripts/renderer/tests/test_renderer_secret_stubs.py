"""The secret stub form in `ansible/filter_plugins/secret_stubs.py`, against the real tree.

Run: uv run pytest scripts/renderer/tests/test_renderer_secret_stubs.py
"""

import json
import re

import pytest

import secret_stubs
from deploy_tools.narrow_lib.filters import filter_names
from lib import yaml_fast
from lib.narrow_git import CannotNarrow
from lib.repo_paths import ANSIBLE
from secret_stubs import (
    NEVER_STUB,
    homelab_secret_stub,
    homelab_stub_keys,
    homelab_unstubbed_keys,
)

SECRETS = ANSIBLE / "vars" / "secrets.yml"
GA = "google_assistant_service_account"
GA_FIELDS = {"project_id", "client_email", "private_key"}
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _file_keys() -> set[str]:
    """Every top-level key of `secrets.yml`, read by line rather than by a YAML parse."""
    return set(re.findall(r"^([A-Za-z0-9_]+):", SECRETS.read_text(), re.M))


def _stub_map() -> dict[str, str]:
    keys = homelab_stub_keys(yaml_fast.safe_load(SECRETS.read_text()))
    return {key: homelab_secret_stub(key) for key in keys}


FILTERS = {"homelab_secret_stub", "homelab_stub_keys", "homelab_unstubbed_keys"}
PLUGIN_REL = "ansible/filter_plugins/secret_stubs.py"


def test_filters_register_as_one_literal_dict_is_clean():
    """The deployer reads the names statically; a refusal there defers every service."""
    source = (ANSIBLE / "filter_plugins" / "secret_stubs.py").read_text()
    assert filter_names(source, PLUGIN_REL) == FILTERS
    assert set(secret_stubs.FilterModule().filters()) == FILTERS


def test_filters_built_any_other_way_is_flagged():
    source = (ANSIBLE / "filter_plugins" / "secret_stubs.py").read_text()
    literal = source[source.index("        return {\n") :]
    built = source.replace(
        literal,
        "        return dict(homelab_secret_stub=homelab_secret_stub)\n",
    )
    assert built != source
    with pytest.raises(CannotNarrow, match="literal dict"):
        filter_names(built, PLUGIN_REL)


def test_stub_keys_are_every_secret_key_but_sops_and_never_stub():
    file_keys = _file_keys()
    assert {"sops", "become_password", GA, "domain"} <= file_keys
    keys = homelab_stub_keys(yaml_fast.safe_load(SECRETS.read_text()))
    assert keys == sorted(file_keys - {"sops"} - NEVER_STUB.keys())
    assert "become_password" not in keys


def test_every_stub_but_the_google_assistant_one_is_a_dns_label():
    stubs = _stub_map()
    bad = {
        key: stub
        for key, stub in stubs.items()
        if key != GA and not (DNS_LABEL.match(stub) and len(stub) <= 63)
    }
    assert not bad
    assert stubs["domain"] == "stub-domain"


def test_google_assistant_stub_is_an_object_of_the_three_fields_the_template_reads():
    stub = homelab_secret_stub(GA)
    fields = json.loads(stub)
    assert set(fields) == GA_FIELDS
    assert all(
        isinstance(value, str)
        and value.startswith("stub-google-assistant-service-account-")
        for value in fields.values()
    )
    assert stub == json.dumps(fields, sort_keys=True)


def test_unstubbed_keys_of_the_full_stub_map_is_clean():
    stubs = _stub_map()
    assert homelab_unstubbed_keys(list(stubs), stubs) == []
    assert homelab_unstubbed_keys(list(stubs), list(stubs.values())) == []


def test_unstubbed_keys_names_exactly_the_changed_or_missing_key_is_flagged():
    stubs = _stub_map()
    changed = stubs | {"domain": "example.org"}
    assert homelab_unstubbed_keys(list(stubs), changed) == ["domain"]
    assert homelab_unstubbed_keys(list(stubs), list(changed.values())) == ["domain"]
    missing = {key: value for key, value in stubs.items() if key != GA}
    assert homelab_unstubbed_keys(list(stubs), missing) == [GA]
    with pytest.raises(ValueError, match="keys but"):
        homelab_unstubbed_keys(["domain", GA], ["stub-domain"])
