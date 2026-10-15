"""qbittorrent's Mullvad account lives in Secret qbittorrent-mullvad-account, projected beside the key.

The account number is all digits and stands alone in its field, so a Flux placeholder would
type it as a number, and target slice R2's PR 7b moved it out of qbittorrent-mullvad into a
Secret of its own. The sidecar still reads FILE__MULLVAD_ACCOUNT from <mount>/account, so the
Deployment's mount must project both Secrets. A volume that projected only the old Secret would
deploy green and leave the mullvad mod with no account file.
"""

from _k8s_render import rendered_docs

ROLE = "qbittorrent"
MOUNT = "/mullvad"


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
