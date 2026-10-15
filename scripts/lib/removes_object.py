"""The `Removes-Object:` line: a commit's declaration that it removes a cluster object on purpose.

Grammar, one object per line: `Removes-Object: <kind>/<namespace>/<name>`, where a
cluster-scoped object leaves the namespace empty, as in `ClusterRole//<name>`. The key is
case-insensitive, as git's trailers are.

Two readers share it. `land.sh`'s direct merge (`land_lib/merge.py`) copies every such line
from the PR's commits into the squash commit's body. `promote.py decide`
(`deploy_tools/object_loss.py`) matches the line anywhere in a commit message, not only in
git's final-paragraph trailer block, so a line reaches it however the squash body is
assembled. docs/flux-deploy-plane.md, *Removing an object on purpose*, is the operator's page.
"""

import re

KEY = "Removes-Object"
REMOVES_OBJECT = re.compile(rf"^{KEY}:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def declared(messages: str) -> frozenset[str]:
    """The objects every `Removes-Object` line in `messages` declares."""
    return frozenset(REMOVES_OBJECT.findall(messages))
