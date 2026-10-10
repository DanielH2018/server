## What changed, and why

The in-use check that runs before a worktree removal (`lib.worktrees.processes_using`) reads `/proc` as the checkout owner, `ubuntu`. The kernel refuses that uid another user's `cwd` and `environ`. Since #3994 the check counts such a process only when it sits inside `user-<ubuntu uid>.slice`. A `claude` process started through `su claude`, `runuser -l claude` or `systemd-run --uid=claude` lands in claude's own slice. It can hold a cwd under `.claude/worktrees/` while the weekly sweep and `fanout_place clean` both read "no holder".

The fix gives only the `/proc` scan root:

- `initial_setup` installs `files/worktree_holders.py` as a root-owned copy at `/usr/local/libexec/worktree-holders` (`root:ubuntu 0750`). A sudoers drop-in, `ubuntu ALL=(root) NOPASSWD: /usr/local/libexec/worktree-holders ""`, allows it with no arguments.
- The helper scans every process. It derives the one directory it reports under from `SUDO_UID` (`~/server/.claude/worktrees`). It prints `pid\tcwd|CLAUDE_PROJECT_DIR|unreadable\tvalue`. Apart from a matched `CLAUDE_PROJECT_DIR` value, nothing from `environ` is printed: Claude processes carry tokens there, and the sweep's output goes to the journal.
- `processes_using` asks the helper first, through `privileged_holders`. Once the helper applies, any non-zero exit, timeout, unparseable line or `unreadable` process refuses the removal (fail closed).
- The helper does not apply when it is absent, when the caller cannot execute it (the 0750 mode doubles as "the sudoers grant is mine"), or when the tree is outside the helper's root. In those cases the #3994 slice rule still runs. When the helper is absent the sweep says so on stderr, and the cron journals that line. The fallback reasoning is a `# DECIDED:` marker in `privileged_holders`.

The install is gated on `has_claude_code`, so it lands on daniel-box and daniel-server. The weekly sweep runs on daniel-box. `fanout_place clean` runs on whichever host ran the batch, and this batch ran on daniel-server. A host with `has_claude_code: false` gets the absent arm, which removes the helper, the sudoers rule and the stamp fragment. The helper is stamped for the setup-drift reader, which runs as root, so mode 0750 does not hide the file from it.

Closes #4170

## Why not the cheaper options

- **Ambient `CAP_SYS_PTRACE` on the cron.** The capability would also reach `uv`, `python` and `git`, as the issue notes.
- **A sudoers rule that takes the tree path as an argument.** In sudoers, `*` matches `/` and `..`, so `ubuntu` could ask root about any directory. The helper takes no arguments and `main` refuses one.
- **Running the helper from the checkout, or under the uv-managed host Python.** Both are writable by non-root users, so a sudoers rule naming either one gives root to whoever edits it. The helper runs as a copy under `#!/usr/bin/python3 -I`.
- **Failing closed when the helper is absent.** Without root, `claude-rc.service` and the claude user's own sessions are always alive and always unreadable. The sweep would then refuse every removal on any host where the apply has not run.

## The distro interpreter trap

`/usr/bin/python3` on these hosts is 3.12.3. `ruff format` targets the repo's 3.14 and rewrote `except (FileNotFoundError, ProcessLookupError):` into the bare PEP 758 form, which 3.12 cannot parse:

```
SyntaxError: multiple exception types must be parenthesized
```

To avoid that, the helper matches `errno` (`ENOENT`, `ESRCH`) instead of a tuple of exception classes. `test_the_helper_parses_on_the_distro_interpreter_is_clean` parses the file with `ast.parse(feature_version=(3, 12))`. Its pair, `test_syntax_newer_than_the_distro_interpreter_is_flagged`, shows that this check rejects the 3.14 form.

## Services touched

None. This is a setup-plane change to `initial_setup` (tag `worktree-sweep`), and no k8s or Pi service is affected.

## Deploy

- [ ] Deployed after merge
- [x] Deliberately deferred, because this batch has a review phase and the pipeline lands it. The deployer's classifier, run on this branch:
  - `narrow_setup.py initial_setup origin/master HEAD` → `ansible-log,cert-expiry,crons,docs,evals,firmware,infra-map,loki_route_witness,prune,secret-rotation,setup_drift,weekly-restart,worktree-sweep`, and `tick_applies_setup_role('initial_setup')` → `True`, so the tick applies it on daniel-box.
  - `remaining_setup_hosts_note` → `initial_setup also reaches daniel-server (not applied by this tick): ssh daniel-server "cd /home/ubuntu/server && git pull --ff-only && ansible-playbook ansible/initial_setup.yml --tags initial_setup"`. It also names daniel-pi, where the change runs only the absent arm.
- [ ] Nothing to deploy

## Verification

- [ ] `probe.py health`: not applicable, because no workload is touched.
- [ ] Exercised the changed behaviour itself: **not run.** The issue's Verify-by needs root and the applied helper, and this session has neither. After the apply, run as root on daniel-box:

  ```
  systemd-run --uid=claude --working-directory=/home/ubuntu/server/.claude/worktrees/<a merged tree> sleep 600
  ```

  Then, as `ubuntu`, run `uv run python scripts/dev/prune_worktrees.py --prune`. It must refuse that tree with `in use by a live process: pid <n> (cwd …)`.

Commands I did run:

- `uv run pytest`: `14859 passed, 69 skipped in 166.37s`. That run was before the `has_claude_code` gate commit. After that commit, `uv run pytest scripts/lib/tests/test_worktrees.py ansible/tests/setup ansible/roles/setup/initial_setup/tests scripts/dev/tests/test_prune_worktrees.py` printed `1348 passed, 1 skipped`.
- `visudo -cf` on the rule line printed `/tmp/wh-sudoers-check: parsed OK`.
- `SUDO_UID=1000 /usr/bin/python3 -I .../worktree_holders.py`, run unprivileged on 3.12.3, printed `1	unreadable	cwd: Permission denied` for each pid, which is the expected result without root.
- The prek hooks passed on both commits, including ansible-lint, ty and the static ratchet tests.

## Blast radius

- [x] This is a **broad** change (`ansible/roles/setup/`). It adds a NOPASSWD root rule on daniel-box and daniel-server. The rule is limited to one argument-free, root-owned script that only reads `/proc`.
- [x] New check: it ships with paired tests. In the helper tests, a foreign cwd is flagged, environ leaks only the project dir, a process root cannot read is flagged and a vanished process is skipped. In the caller tests, a helper answer is used, a failed, garbled or blind helper refuses the removal, and an absent or foreign helper or an out-of-root tree falls back. A path-pin test ties the install `dest`, the sudoers command and `WORKTREE_HOLDERS` to one string. Its red half shows that an argument-accepting rule is refused.

The issue text contained no instructions beyond its own defect.
