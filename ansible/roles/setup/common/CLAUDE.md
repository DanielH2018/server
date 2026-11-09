# `setup/common` — the resolv.conf template two host components render

Not a role a playbook applies. Nothing lists `common` in `initial_setup.yml`, and it has no
`tasks/` and no deploy tag. H7.63 deleted its last task files with their importers: the
setup-drift check's stamp tasks, the host-check timer task and its unit templates, and the
`host_lib.py` copy task. H7.53's census reads this directory on every host, so a new task file
here would read as setup work left to move.

| File | What it gives a caller |
|---|---|
| `templates/resolv.conf.j2` | The resolv.conf `hosts/components/k3s-host-dns/` renders on daniel-box. The Pi's `hosts/components/pi-resolver/` loads the same file through `role: common`. |

`resolv.conf.j2` records nothing. The host packages render it, so the deployer maps it to no
role (`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py:SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES`),
and `land.sh` waits on each host's package instead.

`hosts/lib/host_lib.py` left this role's `files/` in H7.2e. The host packages ship it beside
each consumer script, and `hosts/lib/tests/` tests it. It must stay importable on the host
Python floor (`test_host_python_pin.py`), not only on the repo's 3.15, because it runs under
the pinned host interpreter, never `uv run` against the repo.
