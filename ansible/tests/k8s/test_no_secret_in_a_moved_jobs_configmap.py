"""No ConfigMap of a role whose host cron moved into a CronJob carries a secret's value.

A host script templated its push token into its own body, which was safe in a 0700 file. A
script staged into a ConfigMap is readable by anything that can read ConfigMaps, so each moved
job reads its secrets from its role's Secret through `secretKeyRef` instead
(`docs/target-architecture/host/h2-shrink-host-plane.md`, *No secret in a ConfigMap*).

The test binds every key name in `ansible/vars/secrets.yml` to a unique sentinel value, renders
each moved role's top-level templates with those values, and fails when a ConfigMap's data
holds a sentinel. Key names are plaintext in the encrypted file, so it decrypts nothing. It
reads only ConfigMaps: a role's Secrets carry secret values by design.

Each H2 family PR that moves a role's cron adds that role to `MOVED_ROLES`: configarr at
H2.5b, then crowdsec at H2.4b and H2.4c, janitorr at H2.5c and qbittorrent at H2.5d.

Run: uv run pytest ansible/tests/k8s/test_no_secret_in_a_moved_jobs_configmap.py
"""

from functools import cache

from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from lib import yaml_fast
from secrets_mgmt.rotation_tools import sops_names

MOVED_ROLES = ("configarr", "janitorr")

# The red fixture: the line the retired configarr-health.sh.j2 carried, staged into a
# ConfigMap the way a careless move would stage it.
_LEAKING_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: leaky-health
data:
  health.sh: |
    PUSH_URL="https://kuma/api/push/{token}"
"""


@cache
def sentinels() -> dict[str, str]:
    """Each secrets.yml key name -> a value no template could produce by itself."""
    return {name: f"Zq{i:04d}sentinelZq" for i, name in enumerate(sops_names())}


def leaked(rendered: str, values: dict[str, str]) -> list[tuple[str, str]]:
    """`(ConfigMap name, secret key)` for each sentinel a rendered ConfigMap's data holds."""
    found = []
    for doc in yaml_fast.safe_load_all(rendered):
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        data = "\n".join(str(v) for v in (doc.get("data") or {}).values())
        found += [
            (doc["metadata"]["name"], name)
            for name, value in values.items()
            if value in data
        ]
    return found


@cache
def moved_configmap_renders() -> dict[tuple[str, str], str]:
    """`(role, template)` -> its render at the sentinel values, for each ConfigMap template."""
    out = {}
    for role in MOVED_ROLES:
        for path in sorted((K8S_ROLES / role / "templates").glob("*.yaml.j2")):
            text = render_role_template(role, path.name, sentinels())
            if "kind: ConfigMap" in text:
                out[(role, path.name)] = text
    return out


def test_the_render_reaches_the_known_configmaps() -> None:
    """The named members: configarr's and janitorr's health reader code, staged at H2.5b and
    H2.5c."""
    assert ("configarr", "health-configmap.yaml.j2") in moved_configmap_renders()
    assert ("janitorr", "health-configmap.yaml.j2") in moved_configmap_renders()
    assert len(sentinels()) >= 100


def test_the_sentinels_reach_the_render() -> None:
    """The positive control: the Secret beside the ConfigMap does carry the token's sentinel.

    Without it, a binding that never reached the templates would leave every ConfigMap clean.
    """
    for role in MOVED_ROLES:
        token = sentinels()[f"monitor_bridge_{role}_push_token"]
        assert token in render_role_template(role, "health-secret.yaml.j2", sentinels())


def test_a_moved_roles_configmaps_carry_no_secret_value_is_clean() -> None:
    found = {
        key: leaked(text, sentinels())
        for key, text in moved_configmap_renders().items()
    }
    assert {key: hits for key, hits in found.items() if hits} == {}


def test_a_configmap_carrying_a_push_token_is_flagged() -> None:
    values = sentinels()
    token = "monitor_bridge_configarr_push_token"
    rendered = _LEAKING_CONFIGMAP.format(token=values[token])
    assert leaked(rendered, values) == [("leaky-health", token)]


def test_the_same_token_in_a_secret_is_not_flagged() -> None:
    values = sentinels()
    rendered = _LEAKING_CONFIGMAP.format(
        token=values["monitor_bridge_configarr_push_token"]
    ).replace("kind: ConfigMap", "kind: Secret")
    assert leaked(rendered, values) == []
