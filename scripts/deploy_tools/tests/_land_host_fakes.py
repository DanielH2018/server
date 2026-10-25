"""The host wait's fakes for `build_tools`: no landing test renders a package or reaches Prometheus.

`Fakes.host_targets` is what `host_wait.host_targets` answers for the merge commit, and
`Fakes.host_wait_outcome` is the (verdict, rc, detail) `host_wait.wait_for_hosts` ends the
landing with. None means every target settles. Each call lands in the shared `calls` list.
"""


def host_fakes(f, calls: list) -> dict:
    """The `Tools` fields that stand in for `host_wait`."""

    def host_targets(primary, merge):
        calls.append(("host_targets", (merge,), {}))
        return list(f.host_targets)

    def wait_for_hosts(ln, targets, merge, merged_at):
        calls.append(
            ("wait_for_hosts", ([t.host for t in targets], merge, merged_at), {})
        )
        if f.host_wait_outcome is not None:
            verdict, rc, detail = f.host_wait_outcome
            ln.finish(verdict, rc, detail)

    return {"host_targets": host_targets, "wait_for_hosts": wait_for_hosts}
