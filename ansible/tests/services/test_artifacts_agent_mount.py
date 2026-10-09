"""The artifacts Deployment mounts the agent user's tree only when `artifacts_agent_dir` is set.

An empty `artifacts_agent_dir` has to remove the volume, the mount and the index source
together: a volume without a mount is dead weight, and a mount of a path that does not exist
holds the new pod in ContainerCreating.

Run: uv run pytest ansible/tests/services/test_artifacts_agent_mount.py
"""

from _k8s_render import render_role_template
from lib import yaml_fast

AGENT_DIR = "/var/lib/claude/.claude/artifacts"
MOUNT = "/srv/artifacts/daniel-box-claude"


def _pod(overrides: dict) -> dict:
    text = render_role_template("artifacts", "deployment.yaml.j2", overrides)
    spec = yaml_fast.safe_load(text)["spec"]["template"]["spec"]
    return {"volumes": spec["volumes"], "mounts": spec["containers"][0]["volumeMounts"]}


def _agent(pod: dict) -> tuple[list[dict], list[dict]]:
    volumes = [
        v for v in pod["volumes"] if v.get("hostPath", {}).get("path") == AGENT_DIR
    ]
    mounts = [m for m in pod["mounts"] if m["mountPath"] == MOUNT]
    return volumes, mounts


def test_agent_tree_is_mounted_read_only_and_must_already_exist() -> None:
    volumes, mounts = _agent(_pod({"artifacts_agent_dir": AGENT_DIR}))
    assert len(volumes) == 1 and len(mounts) == 1
    # Directory, never DirectoryOrCreate: kubelet would create it root-owned in the agent's home.
    assert volumes[0]["hostPath"]["type"] == "Directory"
    assert mounts[0]["readOnly"] is True
    assert mounts[0]["name"] == volumes[0]["name"]


def test_an_empty_agent_dir_removes_the_volume_and_the_mount() -> None:
    pod = _pod({"artifacts_agent_dir": ""})
    assert _agent(pod) == ([], [])
    assert not any(m["mountPath"] == MOUNT for m in pod["mounts"])
    assert "artifacts-agent" not in {v["name"] for v in pod["volumes"]}


def test_the_default_follows_whether_the_agent_user_is_enabled() -> None:
    assert _agent(_pod({"claude_code_agent_user_enabled": True}))[0] != []
    assert _agent(_pod({"claude_code_agent_user_enabled": False})) == ([], [])
