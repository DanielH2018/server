"""`ansible/deploy.yml`'s k8s play, for the guards whose subject is that play.

R4.7 deletes this module with the play and those guards. Nothing else reads the play.
"""

from lib import yaml_fast
from _helpers import ANSIBLE

K8S_PLAY_NAME = "Deploy k8s workloads"


def deploy_play() -> dict:
    """deploy.yml's k8s play, selected by its exact name.

    deploy.yml opens with the inert Docker play, so the k8s play is not the first. An
    exact name rather than a substring, so a second play with `k8s` in its name cannot be
    picked up in its place.
    """
    for play in yaml_fast.safe_load((ANSIBLE / "deploy.yml").read_text()) or []:
        if play.get("name") == K8S_PLAY_NAME:
            return play
    raise AssertionError(f"deploy.yml no longer has a play named {K8S_PLAY_NAME!r}")
