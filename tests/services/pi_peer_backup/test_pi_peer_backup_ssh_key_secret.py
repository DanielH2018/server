"""pi-peer-backup's private key lives in Secret pi-peer-backup-ssh-key, projected into /ssh.

The key is multi-line, so no Flux placeholder can carry it, and target slice R2's PR 6 moved it
out of pi-peer-backup-ssh into a Secret of its own. The script still reads /ssh/id, so the
CronJob's /ssh volume must project both Secrets. A volume that projected only the old Secret
would deploy green and fail the nightly run with no identity file.

The other Secret, pi-peer-backup-ssh, pins the Pi's host key from host_vars, the same value
the pi-ssh-name component pins on the k3s nodes, so a re-pin moves both pins at once.
"""

from lib import yaml_fast

from _k8s_render import host_context, render_role_template, rendered_docs

ROLE = "pi-peer-backup"


def _docs(kind: str) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for role, _tpl, doc in rendered_docs()
        if role == ROLE and doc.get("kind") == kind
    }


def test_the_private_key_has_a_secret_of_its_own():
    secrets = _docs("Secret")
    assert set(secrets["pi-peer-backup-ssh-key"]["stringData"]) == {"id"}
    assert "id" not in secrets["pi-peer-backup-ssh"]["stringData"]


def test_the_cronjob_projects_both_secrets_at_the_ssh_mount():
    pod = _docs("CronJob")[ROLE]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (mount,) = [
        m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == "/ssh"
    ]
    (volume,) = [v for v in pod["volumes"] if v["name"] == mount["name"]]
    names = [s["secret"]["name"] for s in volume["projected"]["sources"]]
    assert sorted(names) == ["pi-peer-backup-ssh", "pi-peer-backup-ssh-key"]


def test_the_known_hosts_pin_follows_the_pi_host_vars_key():
    hosts = host_context()["hosts"]
    pi = {**hosts["daniel-pi"], "ssh_host_ed25519_key": "ssh-ed25519 AAAAre-pinned"}
    (secret,) = [
        doc
        for doc in yaml_fast.safe_load_all(
            render_role_template(
                ROLE, "secret.yaml.j2", {"hosts": {**hosts, "daniel-pi": pi}}
            )
        )
        if doc
    ]
    assert secret["stringData"]["known_hosts"] == (
        f"{pi['server_ip']} ssh-ed25519 AAAAre-pinned"
    )
