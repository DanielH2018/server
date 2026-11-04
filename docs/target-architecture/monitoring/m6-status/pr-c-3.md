### PR C-3a and C-3b

#### C-3a, the push libraries

The reads ran at base `764b603be` on 2026-11-04 at about 16:30Z. [#7109](https://github.com/DanielH2018/server/issues/7109)
holds the rulings.

- **The filename grep.** `git grep -l kuma_push.py -- ansible hosts scripts ':!*/tests/*'` listed
  only files this PR edits or deletes.
- **Read 1.** Every check the plan names reports a run after its batch's Cutover-2, except
  `aide-check`. The operator waived that one on 2026-11-04, because read 3 finds no installed
  AIDE script that sources the library. Its next run is 2026-11-09 03:00.
- **Read 2.** The `git grep -n kuma-push-lib` over templates, files and `hosts` printed no
  script that sources the library. Each hit is a comment, the library, its component or a
  `legacy:` row.
- **Read 3.** On each host the grep listed the library's own copies, including the Python
  `kuma_push.py` copies under `/opt` and `/usr/lib/homelab/builder` on daniel-box, and
  comment-only hits in `homelab-check-lib.sh`, `setup-drift-lib.sh` and
  `etcd-drill-guest-run.sh`. On the Pi it also matched `/usr/lib/homelab/manifest.json`, the
  package manifest that names the library's path. Four retained daniel-box release trees under
  `/opt/homelab/releases/k3s-disk-health/` still hold the `source` line.
  `/opt/homelab/current/k3s-disk-health` points at `45a8eb0ef73d`, which does not.
- **Unreadable scripts.** `github-ruleset-drift.sh`, `github-interaction-limit.sh` and
  `registry-gc.sh` on daniel-box return `Permission denied`, so the rendered source stands in
  for them. `git grep -c kuma-push-lib` over those three and `eval-run` exits 1, which means no
  hit.
- **Package versions.** daniel-box and daniel-server run `0.6733+ge35604c7d422` and daniel-pi
  runs `0.6738+g53142821af01`. B9's Cutover-2, `35edf86f1`, is an ancestor of both SHAs.

Two changes went beyond the plan's list, and the plan now names both. `render_records.py` read
`kuma_push` through the re-export, which the filename grep cannot see. The dropped record task
would have left the `kuma-push-lib` fragment behind as permanent drift, so the absent task removes
it too.
