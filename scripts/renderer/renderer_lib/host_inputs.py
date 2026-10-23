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
    "ansible/filter_plugins/",
    "scripts/lib/",
    "scripts/renderer/",
    # They decide the environment `uv run` renders in.
    "pyproject.toml",
    "uv.lock",
    # Host mode removes its key names (`host.secret_names`).
    "ansible/vars/secrets.yml",
    # Two of `host_sources.SHARED_FILES`, which H3.8b and M6 B5 copy into the Pi's package.
    "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh",
    "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh",
    # Past the plan's list, found by the audit test: ansible-core reads `ansible.cfg` from the
    # render's working directory, and `homelab_render.py` imports the other two at load, for
    # cluster mode.
    "ansible.cfg",
    "scripts/deploy_tools/render_targets.py",
    "scripts/secrets_mgmt/cluster_vars_check.py",
    "scripts/secrets_mgmt/pending_retirement.py",
    # Also past the plan's list, imported at load through `ansible/filter_plugins/` and
    # `scripts/lib/`. A warm bytecode cache hid them, because the loader then opens only the
    # untracked `.pyc`; the audit runs with none.
    "ansible/roles/setup/common/files/host_lib.py",
    "ansible/roles/setup/common/files/kuma_push.py",
    "ansible/roles/setup/k3s/files/longhorn_lib/longhorn_backups.py",
)
