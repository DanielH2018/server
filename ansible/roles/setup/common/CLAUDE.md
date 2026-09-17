# `setup/common` — task files and one shared module the other setup roles import

Not a role a playbook applies. Nothing lists `common` in `initial_setup.yml`, and it has no
`tasks/main.yml`, no deploy tag and nothing to run on its own: it is the include-only home for
the pieces several setup roles need byte-identical. A caller reaches it with
`ansible.builtin.import_tasks: "{{ role_path }}/../setup/common/tasks/<file>.yml"` (one more
`../` from `roles/k8s/`) and passes the variables the file's header names.

| File | What it gives a caller |
|---|---|
| `tasks/release_bin.yml` | Deploys a group of host scripts as a versioned release under `/opt/homelab/releases/<group>/<sha>/`, with `/usr/local/bin/<name>` a symlink through `current/`. Provenance is readable and rollback is one symlink. |
| `tasks/install_host_lib.yml` | Copies `files/host_lib.py` beside a consumer script so `import host_lib` resolves — every consumer is a directly-invoked script with only its own directory on `sys.path`. |
| `tasks/stamp_render.yml` | Records the source checksum of a group of rendered templates, one fragment per group, so a tag-scoped run leaves the other groups' fragments stale and the drift check reports the truth. |
| `tasks/stamp_deployed.yml` | Records `{live, src}` pairs for `copy:`-deployed artifacts, so `manifest-prune-check.sh`'s second arm can compare live bytes against the repo in either direction. |
| `files/host_lib.py` | `parse_env_file`, `atomic_write`, `discord_post` (the Cloudflare-1010 User-Agent, 2xx-only success). The one copy of Python shared across setup-role host scripts; tested in `tests/`. |
| `templates/resolv.conf.j2` | The resolv.conf `k3s` (`node.yml`) and `optimize_pi` both render. |

Each task file's header carries the reasoning and the variable contract; read it before
adding a caller. `host_lib.py` must stay importable on the host Python floor
(`test_host_python_pin.py`), not only on the repo's 3.14 — it runs under the pinned host
interpreter, never `uv run` against the repo.
