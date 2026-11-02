### B6c registry GC

B6c joined M6 on 2026-11-02 (#6926). D5's registry PRs retired the push store's GC phase
only, and the mirror phases of `registry-gc.sh` kept pushing to the `Registry GC` tile, so
C-1a's gate could never clear while the plan left the push to D5.

**The shadow PR** is the PR that adds this file. It changes four things:

- `registry-gc.sh.j2` sources `homelab-check-lib.sh` and calls
  `write_check_textfile registry-gc` in `finish`, before the push and on every run, with a
  `DEADLINE_S` of 612000, the tile's interval. A run with no token or a refused push still
  writes the file.
- `rules/registry.yaml` gains `RegistryGcFailed` and `RegistryGcNotReporting`, both
  `warning` and `shadow: "true"`, with promtool cases in `registry.test.yaml`.
- `KUMA_RETIRING_TAGS` gains `registry-gc`.
- `kuma-cutover.yaml` gains the `[340h10m]` term for `Registry GC`.

The tick applies the script change (`k8s_host_files`), and Flux applies the two k8s halves
through `promote`. NotReporting fires in shadow from its deploy until the first Sunday run
writes the file.

**Still to come, in order:**

1. The soak read, one week after the first Sunday run that writes the textfile.
2. Cutover-1a: delete `registry-gc.json` and the `^registry-gc$` status-page pattern from
   `uptime-kuma`, and move `registry_gc_push_token` to `PENDING_RETIREMENT`. The tag must
   have been in the deployed `KUMA_RETIRING_TAGS` for a week first.
3. Cutover-1b: drop `shadow` from both rules and remove the `Registry GC` term.
4. Cutover-2: drop the push, `PUSH_TOKEN`, `KUMA_HOST` and the `kuma-push-lib.sh` source from
   `registry-gc.sh.j2`. The healthchecks.io dead-man stays.
   `.claude/hooks/tests/test_block_protected_bash.py` asserts the token in the script's
   protected paths, so the same PR updates it. With no token in the render, the script is no
   longer a STUB, and `probe.py applied` compares it (#6917).
5. The token PR, a week after Cutover-1b, retires `registry_gc_push_token`. C-1a waits for it.
