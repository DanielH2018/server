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
    # The ciphertext a host package carries (`host_outputs.finish`): a change to a host's file
    # changes that host's package (H4.2, H4.5).
    "ansible/vars/hosts/",
    # Two of `host_sources.SHARED_FILES`, which H3.8b and M6 B5 copy into the Pi's package.
    "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh",
    "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh",
    # What a component's `role:` renders (`host_sources.ROLE_SOURCES`): the role's defaults and
    # the template daniel-server's package renders in place (H4.3a).
    "ansible/roles/setup/nut_host/defaults/main.yml",
    "ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2",
    # Past the plan's list, found by the audit test: ansible-core reads `ansible.cfg` from the
    # render's working directory, and `homelab_render.py` imports the other two at load, for
    # cluster mode.
    "ansible.cfg",
    "scripts/deploy_tools/render_targets.py",
    "scripts/secrets_mgmt/cluster_vars_check.py",
    "scripts/secrets_mgmt/pending_retirement.py",
    # `host_lib.py`, `kuma_push.py` and `longhorn_lib/longhorn_backups.py`, which the filter
    # plugins and `scripts/lib/` import at load, sit under `hosts/` and `scripts/lib/` since
    # H7.2e, so the two directory members above already cover them.
)
