# game-stats-lib (k8s) — the stats_lib.py both game-stats roles share

No standalone deploy tag and no `containers_list` entry — like `k8s/volume-claim` and
`k8s/manifests`, this role ships no workload of its own. It is reached only via
`ansible.builtin.import_tasks` from a caller's own tasks (never `--tags game-stats-lib`; that
selects nothing, since this role has no `tasks/main.yml` to run standalone).

## What it holds
`files/stats_lib.py` — the Loki-tail → Prometheus-exporter skeleton `valheim-stats` and
`terraria-stats` were built as two independent forks of, and later found to share ~250
non-comment lines of (see stats_lib.py's own docstring for the exact function list). It has
no per-game state and takes every game-specific bit — URLs, the parser's `apply_fn`, the
render/log callbacks — as an argument, so it needs no test doubles patched onto it.

## How it ships
Both stats roles run as `python:3.14-alpine` pods with `stats_lib.py` mounted alongside their
own entry script by a ConfigMap, not on a host with a repo checkout — a directly-invoked
script gets only its own directory on `sys.path`, so a copy has to be staged beside each
consumer. `tasks/stage.yml` is that shared step (the same shape
`roles/setup/common/tasks/install_host_lib.yml` uses for `host_lib.py`, adapted for
ConfigMap staging rather than a host `/opt` install), and it owns both halves of the
shipment: a caller `import_tasks`s it with `game_stats_lib_dest_dir` set to its own staging
directory, and the include copies the file there AND sets `game_stats_lib_from_file` to the
`--from-file=stats_lib.py=<dir>/stats_lib.py` argument for that copy. The caller
interpolates that fact into its own `kubectl create configmap` command and never spells
`stats_lib.py` itself — the node-local copy alone is not enough, since what actually
reaches the pod is the ConfigMap, and until #2055 a caller could stage the file and forget
the entry.

`ansible/tests/k8s/test_game_stats_lib_ships_through_the_include.py` is the guard: the fact
names exactly the file the copy stages, every role whose `files/` imports `stats_lib`
includes this file, and every caller's ship list interpolates the fact.

## Editing
Logic + its own tests: `files/stats_lib.py` / `tests/test_stats_lib.py` (`uv run pytest
ansible/roles/k8s/game-stats-lib/tests`). A behaviour change here affects both consumers —
run `uv run pytest ansible/roles/k8s/valheim-stats/tests ansible/roles/k8s/terraria-stats/tests`
too before committing.
