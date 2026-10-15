"""qbittorrent's Mullvad account lives in Secret qbittorrent-mullvad-account, projected beside the key.

The account number is all digits and stands alone in its field, so a Flux placeholder would
type it as a number, and target slice R2's PR 7b moved it out of qbittorrent-mullvad into a
Secret of its own. The sidecar still reads FILE__MULLVAD_ACCOUNT from <mount>/account, so the
Deployment's mount must project both Secrets. A volume that projected only the old Secret would
deploy green and leave the mullvad mod with no account file.
"""

from lib import yaml_fast
from _helpers import ANSIBLE
from _k8s_render import rendered_docs
from validate.k8s_manifests import make_env, register_ansible_filters

ROLE = "qbittorrent"
MOUNT = "/mullvad"
ACCOUNT_TEMPLATE = (
    ANSIBLE / "roles/k8s/qbittorrent/templates/mullvad-account-secret.yaml.j2"
)
QUOTED = 'account: "{{ mullvad_account }}"'


def _docs(kind: str) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == ROLE and doc.get("kind") == kind
    }


def test_the_account_has_a_secret_of_its_own():
    secrets = _docs("Secret")
    assert set(secrets["qbittorrent-mullvad-account"]["stringData"]) == {"account"}
    assert set(secrets["qbittorrent-mullvad"]["stringData"]) == {"key"}


def test_the_deployment_projects_both_secrets_at_the_mount():
    pod = _docs("Deployment")[ROLE]["spec"]["template"]["spec"]
    (sidecar,) = [c for c in pod["initContainers"] if c["name"] == "wireguard"]
    env = {e["name"]: e.get("value") for e in sidecar["env"]}
    assert env["FILE__MULLVAD_ACCOUNT"] == f"{MOUNT}/account"
    (mount,) = [m for m in sidecar["volumeMounts"] if m["mountPath"] == MOUNT]
    (volume,) = [v for v in pod["volumes"] if v["name"] == mount["name"]]
    names = [s["secret"]["name"] for s in volume["projected"]["sources"]]
    assert sorted(names) == ["qbittorrent-mullvad", "qbittorrent-mullvad-account"]
    assert volume["projected"]["defaultMode"] == 0o400


def _account_as_sops_types_it(source: str):
    """`stringData.account` from `source` rendered with mullvad_account an int, as SOPS types it.

    The tree-wide renders bind every secret to a string stub, so they cannot see this.
    """
    env = register_ansible_filters(make_env([]))
    text = env.from_string(source).render(k8s_namespace="media", mullvad_account=123456)
    return yaml_fast.safe_load(text)["stringData"]["account"]


def test_an_int_account_renders_as_a_string():
    assert _account_as_sops_types_it(ACCOUNT_TEMPLATE.read_text()) == "123456"


def test_a_to_json_account_renders_as_an_int():
    source = ACCOUNT_TEMPLATE.read_text()
    # Without the quoted line the replace below is a no-op, and this test would render the
    # template unchanged rather than the variant it exists to reject.
    assert QUOTED in source
    variant = source.replace(QUOTED, "account: {{ mullvad_account | to_json }}")
    assert _account_as_sops_types_it(variant) == 123456
