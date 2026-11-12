"""`HOST_INPUTS`: every repo path a host render reads (H3, *Steps*, H3.9a item 1).

A change under none of them leaves every host package's render as it was, which is what lets
the landing tell a PR that may reach a host from one that cannot without rendering. Each
member is a prefix relative to the repo root: one ending `/` is a directory, any other is one
file.

The module imports nothing, so the landing can read the constant without loading ansible,
jinja2 or the renderer. `test_renderer_host_inputs.py` records every tracked file a real host
render opens and fails on one under no member.
"""

HOST_INPUTS = (
    "hosts/",
    "ansible/inventory/",
    "cluster/services.yml",  # the k8s host's `containers_list` (`render_guard.host_layer`)
    "ansible/filter_plugins/",
    "scripts/lib/",
    "scripts/renderer/",
    # They decide the environment `uv run` renders in.
    "pyproject.toml",
    "uv.lock",
    # Host mode removes its key names (`host.secret_names`).
    "ansible/vars/secrets.yml",
    # The ciphertext a host package carries (`host_outputs.finish`): a change to a host's file
    # changes that host's package (H4.2, H4.5).
    "ansible/vars/hosts/",
    # The drill script daniel-server's hypervisor component ships (`host_sources.SHARED_FILES`).
    "scripts/backup/etcd_restore_drill.sh",
    # Past the plan's list, found by the audit test: `homelab_render.py` imports it at load, for
    # cluster mode.
    "scripts/secrets_mgmt/cluster_vars_check.py",
    # `host_lib.py` and `longhorn_lib/longhorn_backups.py`, which the filter
    # plugins and `scripts/lib/` import at load, sit under `hosts/` and `scripts/lib/` since
    # H7.2e, so the two directory members above already cover them.
)
