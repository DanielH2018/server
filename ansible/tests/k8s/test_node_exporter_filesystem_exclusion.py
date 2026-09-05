"""node-exporter's filesystem exclusion must expose CSI global mounts and nothing more (#1243).

The 50-minute silent outage this closes was a two-part blind spot: `node_filesystem_readonly`
was already scraped, but excluded under `var/lib/kubelet` wholesale, and
`checks.storage.check_kubelet_plugin_readonly` reads green on an EMPTY vector by design (an
absent series there genuinely means no CSI global mount is read-only — see that check's
docstring). Which means a regex that silently widens back to excluding
`var/lib/kubelet/plugins` is invisible to the check itself: node-exporter stays up, the family
just goes empty, and the monitor reads healthy forever. This is the only thing standing between
that regression and #1243 recurring unnoticed, so it is asserted directly against the compiled
pattern rather than left to the check's own (correct, but blind-to-this) empty-is-fine logic.

Run: uv run pytest ansible/tests/k8s/test_node_exporter_filesystem_exclusion.py
"""

import re

from _k8s_render import rendered_docs

_ARG_PREFIX = "--collector.filesystem.mount-points-exclude="

_MUST_STAY_EXCLUDED = (
    "/proc",
    "/sys",
    "/dev",
    "/host",
    "/etc",
    "/var/lib/kubelet/pods",
    "/var/lib/kubelet/pods/abc-123/volumes/kubernetes.io~empty-dir/cache",
)

# The whole point of #1243's fix: these must be SCRAPED, not excluded.
_MUST_STAY_INCLUDED = (
    "/var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/abc/globalmount",
    "/var/lib/kubelet/plugins_registry",
)


def _exclusion_pattern() -> re.Pattern:
    for role, _tpl, doc in rendered_docs():
        if role != "node-exporter" or doc.get("kind") != "DaemonSet":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for arg in container.get("args", []):
                if arg.startswith(_ARG_PREFIX):
                    return re.compile(arg[len(_ARG_PREFIX) :])
    raise AssertionError(
        "node-exporter's DaemonSet carries no --collector.filesystem.mount-points-exclude "
        "arg — the cardinality-bounding exclusion this test guards no longer exists"
    )


def test_the_deployed_pattern_still_excludes_the_unbounded_paths():
    pattern = _exclusion_pattern()
    for path in _MUST_STAY_EXCLUDED:
        assert pattern.match(path), "%s should be excluded but is not: %r" % (
            path,
            pattern.pattern,
        )


def test_the_deployed_pattern_no_longer_excludes_csi_global_mounts():
    pattern = _exclusion_pattern()
    for path in _MUST_STAY_INCLUDED:
        assert not pattern.match(path), (
            "%s is excluded (%r) — this is the #1243 regression: an absent "
            "node_filesystem_readonly series here reads as healthy, not as blind"
            % (path, pattern.pattern)
        )


def test_the_pre_1243_pattern_would_have_hidden_the_csi_global_mounts():
    """THE RED PROOF: the OLD wholesale var/lib/kubelet exclusion must fail the test above.

    Without this, `test_the_deployed_pattern_no_longer_excludes_csi_global_mounts` could pass
    by construction — proving nothing about whether the fix is actually narrower than before.
    """
    old_pattern = re.compile(r"^/(sys|proc|dev|host|etc|var/lib/kubelet)($|/)")
    for path in _MUST_STAY_INCLUDED:
        assert old_pattern.match(path), (
            "the pre-#1243 pattern fixture no longer reproduces the bug it is meant to prove "
            "existed — %s" % path
        )
