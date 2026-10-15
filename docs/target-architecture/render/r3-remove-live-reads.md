# R3 — Remove the pre-apply live reads

Status: DONE

PR 1, pi-peer-backup's committed host key: [#4903](https://github.com/DanielH2018/server/pull/4903).

PR 3, authelia's digest assert: [#4902](https://github.com/DanielH2018/server/pull/4902).

PR 2, n8n's annotations: [#4925](https://github.com/DanielH2018/server/pull/4925).

PR 4's one-time copy: [#5096](https://github.com/DanielH2018/server/pull/5096). The operator
ran it on 2026-10-15, and it landed ahead of PR 4 with D2.6d's token, so only one branch
changed the SOPS ciphertext. PR 4's base therefore carries the three `_argon2` keys, and PR 4
commits no SOPS change of its own. The operator's checks read each stored value as matching
the assert's PHC pattern and the three as distinct.

PR 4, authelia's digests from SOPS: [#5104](https://github.com/DanielH2018/server/pull/5104).
Before the merge, the operator's base and head render records both read `secret_digest`
`554df31b…adef42`, and the head's dry run ran the digest assert `ok`. It deployed at
`4d29bc7e5` in an operator-attended window on 2026-10-15. `probe.py health authelia` passed,
fresh first-factor logins as the operator and as `claude-ui` succeeded, the operator logged in
from a browser, and `pytest -m ui -k grafana` read `6 passed`.

PR 5, the census and the stub-file check: [#5119](https://github.com/DanielH2018/server/pull/5119).
It deleted `PINNED_LIVE_FACTS` and landed at `3273dde27`. The exit criterion holds at that
commit. The stub-file key test and the live-read census passed in CI. The operator's
shadow pair at `3273dde27` printed `58 tags, 325 files, 0 differences`.

PRs 1 and 3 landed without their operator-only pre-merge checks under *Verification*. The orchestrator's briefs omitted them, and each check's property was read after the deploy instead. PR 3's assert ran unstubbed in the real deploy and returned `ok` on the live digests at 2026-10-13 18:12:00. PR 1's release records show the same `secret_digest`, `54b03051…`, before (`8a28ede4`) and after (`d98d8752`), so the Secret's bytes did not move. PR 4's checks stay pre-merge.

Depends on: [R1](r1-shadow-render.md)

Unblocks: D5, which moves every role to Flux and so needs every role to render from git alone

## Goal and exit criterion

After R3, no value a k8s template reads comes from live state. The design named two such
reads, pi-peer-backup's host key and n8n's registry digest. Measuring the tree at
`70591c307` found a third: authelia reuses its users' password digests from the live Secret.
R3 removes all three, so C1's "no live reads" holds for every role, and R1's shadow pair pins
no fact. Ansible stays the only writer to the cluster.

**Exit criterion.** Four checks, all green at one master commit:

1. **Nothing is pinned.** The file that `scripts/renderer/shadow_diff.py stubs` writes holds
   exactly R1's secret-key stub map plus the sentinel `homelab_secret_stubs`. A test asserts
   that the two key sets are equal. R1's `PINNED_LIVE_FACTS` in
   `scripts/renderer/renderer_lib/task_inputs.py` is deleted, with the code that writes it into
   the stub file and the code that reads it back.
2. **The shadow pair is still zero-diff** with that file, repeating R1's procedure.
3. **A census finds no live read feeding a template the render reads.** No name that a
   k8s-play task sets reaches such a template unless the renderer derives it. The census
   follows names through defaults, inventory vars, `include_role` `vars:`, `set_fact`
   expressions and the names a `vars` lookup resolves to, until no name is added. Its named
   members, its accepted lookup shapes and its red fixtures are under *The census* and
   Verification.
4. **The three roles behave as before.** pi-peer-backup's nightly run pushes `up`, n8n runs
   the image whose content tag the renderer computes, and all three Authelia users log in.

## Baseline at `70591c307`

### pi-peer-backup

- **pi-peer-backup reads the Pi over SSH before it renders.** In
  `ansible/roles/k8s/pi-peer-backup/tasks/main.yml`, the task "Read daniel-pi's SSH host key for
  the known_hosts pin" slurps `/etc/ssh/ssh_host_ed25519_key.pub` from daniel-pi into the
  register `pi_peer_backup_hostkey`. "Compose the known_hosts pin line" builds
  `pi_peer_backup_known_hosts_line` from `hostvars['daniel-pi'].ansible_host` and the key's
  first two fields. `templates/secret.yaml.j2` writes it as `known_hosts`. The task hard-fails
  when the Pi is unreachable.
- **The same task file also writes to the Pi.** "Install the forced-command wrapper on
  daniel-pi" and "Authorize the pull key on daniel-pi" are host writes. They are not reads that
  feed a template, so R3 leaves them for [R4](r4-delete-k8s-ansible-path.md).

### n8n

- **n8n stamps a registry digest.** `n8n/templates/deployment.yaml.j2` and
  `deployment-runners.yaml.j2` each render a `checksum_annotation('image', ...)` from
  `k8s_built_images`. image-builder fills that fact by reading the registry. The template
  comment records the incident it answered: on 2026-09-02 a pushed image never rolled out.
  `test_n8n_image_stamp.py` in `ansible/tests/services/` holds the annotation.
- **The content tag already moves n8n's image reference.** `n8n_k8s_image` and
  `n8n_k8s_runners_image` in `n8n/defaults/main.yml` read
  `k8s_built_image_tags.get('n8n', 'latest')` and its `n8n-runners` twin. The tag is computed
  from the build inputs, before the render, so a changed build input changes the pod template.
- **The content tag postdates the incident.** Commit `70dfd2387` (2026-09-22) added
  `image_builder_content_tag`, three weeks after 2026-09-02. On 2026-09-02 n8n's Deployment
  named the mutable `:latest`, so a pushed but unrolled image left the spec unchanged.
- **A rebuild under an unchanged content tag is caught only by the drift gate.**
  `image_builder_force=true` rebuilds with unchanged inputs, so it pushes under the same
  `sha-…` tag (`ansible/roles/k8s/image-builder/CLAUDE.md`, *Notable*). `k8s_rebuilt_images`
  fills only when the pre- and post-build digests differ. `post_tasks/k8s_image_drift_gate.yml`
  compares each running pod's `imageID` with the registry digest and fails the play on a
  mismatch. For n8n alone, the `checksum/image` annotation also rolls the pod in that case.

### authelia

- **authelia reads its own Secret back.** "Look for an existing Authelia users database in the
  cluster" reads Secret `authelia-config`. Three "Reuse the … password hash" tasks lift each
  user's argon2 digest out of it. When one is missing, a "Hash the … password" task starts a
  one-shot pod to mint a digest. `templates/config-secret.yaml.j2` writes the digests as
  `authelia_password_hash`, `authelia_claude_password_hash` and
  `authelia_agent_password_hash`. `authelia_k8s_rehash_passwords` in `authelia/defaults/main.yml`
  forces a re-mint, which is how a password rotation takes effect today.
- **Four checks guard the digests.** "Fail if the claude-ui credentials are missing" and "Fail
  if the claude-agent credential is missing" stop `authelia crypto hash generate` hashing an
  empty plaintext. The three "Extract … digest" tasks take the `$argon2…` run out of the CLI
  output with `regex_search`. Their comment gives the reason: the output carries a `Digest: `
  prefix, and `kubectl run --attach` interleaves its own lines. "Fail if no password hash could
  be obtained" refuses an empty digest.
- **Authelia decodes each stored digest.** The docstring of
  `ansible/tests/services/test_authelia_argon2_parameters_agree.py` records that verification
  reads the algorithm and parameters off each digest's PHC prefix (`crypt.Decode`). The same
  test holds the mint flags against the hashing block in `config-secret.yaml.j2`. It finds the
  mint commands through `HASH_TASK_PODS`, the pod names `authelia-hash` and
  `authelia-hash-claude`.
- **A hashed credential in SOPS has precedent.** `authelia_client_password_hash`, the OIDC
  client secret's digest, is a SOPS key with an entry in `ansible/secret_rotation.yml`.
- **The plaintext passwords stay in use.** `authelia_password`, `authelia_claude_password` and
  `authelia_agent_password` are in `ansible/secret_rotation.yml`, and `authelia_password` is
  `source: record`. `scripts/diagnostics/ui_login.py` reads the operator's and claude-ui's
  plaintext to log in. The `claude_code` setup role renders the agent's plaintext into the agent
  user's home, and `tasks/agent_homelab_ui.yml` there asserts it is non-empty.
- **Five files document the rehash flag as the rotation step.** `git grep -n rehash` finds it in
  `ansible/roles/k8s/authelia/CLAUDE.md`, `ansible/roles/k8s/authelia/defaults/main.yml`,
  `docs/authelia-sessions-and-crowdsec-init.md`, `docs/claude-agent-user.md` and
  `docs/secret-rotation.md`.
- **D5 also plans this conversion.** `deploy/d5-bulk-move.md`, Open question 4, converts
  authelia's digest read in authelia's own D5 PR "unless R3 takes it". R3 takes it (Open
  question 2).

### Task-computed template inputs

- **Task-computed inputs reach templates through defaults as well as directly.**
  `k8s_built_image_tags` appears in no `templates/` file. Nine roles' `defaults/main.yml` read
  it 11 times (`git grep -c k8s_built_image_tags`), and the templates read the `*_k8s_image`
  names those defaults define. `game-stats/tasks/stage.yml` sets `game_stats_lib_checksum`, and
  a `set_fact` folds it into the two `game_stats_*_script_checksum` facts that the templates
  read. pi-peer-backup's read has the same shape: a `register`, then a `set_fact`, then the
  template.
- **The derivable inputs read only the tree.** The content tags, the four script checksums,
  `kuma_declarations` and `janitorr_dry_run` read no live state. R1 computes them.
- **Templates call four lookup plugins, and two of them choose their target at render time.**
  A `git grep -ohE` for `(lookup|query)\(['"][a-z_.]+` over the k8s templates, role defaults,
  `ansible/templates/` and inventory prints 35 `lookup('file`, 21 `lookup('template`, one
  `lookup('ansible.builtin.template`, one `lookup('pipe` and four `lookup('vars`. One of the
  four `vars` hits is comment text in `monitor-bridge/templates/env-secret.yaml.j2`, so a
  Jinja parse sees three `vars` calls. `scripts/lib/k8s_yaml.py:make_lookup` implements
  exactly these four plugins.
    - `homepage/templates/icons-configmap.yaml.j2` calls `lookup('pipe', 'base64 -w0 ' ~
      playbook_dir ~ ...)` over a literal list of three PNGs in the role's `files/`. It reads
      the tree. `make_lookup` implements `pipe` for the `base64 -w0 <path>` shape only.
    - `monitor-bridge/templates/env-secret.yaml.j2` and
      `uptime-kuma/templates/static-monitors.yaml.j2` call `lookup('vars', check.token)`. The
      loop runs over `CHECKS` in `monitor-bridge/files/check_table.py`, which
      `ansible/filter_plugins/py_table.py` parses without running it. Each row's `token`
      names its push-token secret. The first row's is `monitor_bridge_disk_push_token`.
    - `observability/templates/prometheus.yaml.j2` calls `lookup('vars', job.replicas_var)`
      over `containers_list | scrape_jobs(k8s_namespace)`
      (`ansible/filter_plugins/scrape_jobs.py`). The one `replicas_var` at this commit is
      `terraria_k8s_replicas`, in daniel-box's host_vars, and
      `ansible/inventory/group_vars/all.yml` sets it to `0`.
    - Both plays in `ansible/deploy.yml` set `gather_facts: false`.
- **One task-set fact reaches a template that the render does not read.**
  `image-builder/templates/build-job.yaml.j2` names `image_builder_content_tag`, which a
  `set_fact` in `image-builder/tasks/main.yml` sets. image-builder's own tasks apply that Job.
  `scripts/lib/k8s_roles.py:resolved_manifest_files` leaves out every one-off `-job.yaml`
  (`JOB_MANIFEST_SUFFIX`), and R1's out-of-render census records image build Jobs.
- **R1 already guards a check on the stub sentinel.** R1's PR 4 guards sonarr's two reads and
  two asserts on `homelab_secret_stubs`, so a stubbed dry run skips them.

## Design

### pi-peer-backup: commit the Pi's public host key

A new file, `ansible/roles/k8s/pi-peer-backup/files/daniel-pi-ssh-host-ed25519.pub`, holds the
Pi's public host key. A public key is safe to commit. `templates/secret.yaml.j2` builds the
`known_hosts` line from `hostvars['daniel-pi'].server_ip` and the file's first two fields,
through `lookup('file')`. `server_ip` is a literal in the Pi's host_vars, and
`scripts/lib/render_context.py`'s `hostvars` carries it. The Pi's `ansible_host` is a Jinja
expression over that same value, so the rendered line does not change. The slurp and the
`set_fact` go.

The scheduled check is the nightly CronJob itself. It runs with `StrictHostKeyChecking=yes`
against the committed pin, so a changed host key fails that night's run. Today that failure
reads as a generic rsync error. `files/pull-pi-peers.sh` gains a branch for it: ssh's exit 255
with `Host key verification failed.` pushes `down` with the message `daniel-pi host key
differs from the committed pin`. The message then names the fix: re-read the key and commit it.

### n8n: drop the digest annotations

Both `checksum_annotation('image', ...)` lines go, with their comment blocks. n8n is then
covered the way the other eight built images are.

- **Changed build inputs.** The content tag covers them. That includes the 2026-09-02 shape: a
  run pushes an image built from changed inputs and dies before the roll. The re-run renders
  the new content tag, the pod template changes, and the apply rolls the pod.
- **Unchanged inputs, new bytes.** A forced rebuild pushes new bytes under the old content
  tag. If the play dies between the push and the roll, the re-run moves neither the digest nor
  the tag, so nothing rolls. `post_tasks/k8s_image_drift_gate.yml` then fails the re-run and
  names the image. Its printed remedy, `-e image_builder_force=true`, rolls the pod only when
  that rebuild produces yet another digest. When it does not, the operator rolls both
  Deployments (**operator-only**, a write verb):
  `sudo k3s kubectl -n homelab rollout restart deploy/n8n deploy/n8n-runners`.
- **Under Flux the case cannot arise.** D5.4 pushes `ghcr.io/danielh2018/<name>:<content tag>`
  only when that tag does not exist yet. It also replaces `image_builder_force` with a
  committed `rebuild_stamp` that moves the tag. So no bytes change under an unchanged tag. The
  drift gate must stay until every built image is under D5.4. R4 depends on D6, and D6 on D5,
  which gives that order; R4 records it (cross-slice note).

`k8s_built_images` stays, because the drift gate reads it.

### authelia: the digests move into SOPS

Three SOPS keys hold the digests: `authelia_password_argon2`,
`authelia_claude_password_argon2` and `authelia_agent_password_argon2`.
`config-secret.yaml.j2` reads them directly. R3 fills them by copying the live digests, not by
minting new ones, so the rendered Secret does not change. The read, reuse, hash and extract
tasks go, and so does `authelia_k8s_rehash_passwords`. `defaults/main.yml` gains empty
stand-ins for the two managed-user keys, as it has for their plaintexts. It loses the
plaintext stand-ins, which the role no longer reads.

**A deploy-time assert replaces the deleted checks.** One task, "Fail if a stored Authelia
digest is malformed", runs before the manifests include, with `no_log: true` and
`quiet: true`. Its `when` skips it under `homelab_secret_stubs` (below). For each user that
its `authelia_k8s_manage_*_user` flag enables, it requires that the digest:

- matches `^\$argon2id\$v=19\$m=65536,t=3,p=4\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$`, which
  is the PHC form of the hashing block's `memory`, `iterations` and `parallelism`, a 16-byte
  salt and a 32-byte key;
- differs from every other managed user's digest.

The assert rejects an empty value, a `Digest: ` prefix, surrounding whitespace, a pasted
plaintext and a digest minted with other parameters. It fails the deploy before the apply, so
a malformed value stops the play instead of breaking Authelia's load under `Recreate`. The
"Fail if the claude-ui credentials are missing" assert keeps only its
`authelia_claude_totp_secret` clause, which the TOTP seeding task still reads. The claude-agent
plaintext assert goes, because the `claude_code` role asserts that plaintext itself.

**The digest predicate** is the assert's two bullets, applied to the digest of each user the
render includes. Two definitions of that user set agree. The assert reads the
`authelia_k8s_manage_*_user` flags, and a successor outside Ansible sees the digest keys the
render references. In `config-secret.yaml.j2`, the operator's `password:` line is unconditional,
and the `claude-ui` and `claude-agent` lines sit inside
`{% if authelia_k8s_manage_claude_user %}` and `{% if authelia_k8s_manage_agent_user %}`.
PR 4 keeps those gates, so both definitions name the same users. R3 defines the
predicate here, and the Ansible assert is its only implementation while Ansible deploys
authelia. Its successor after D5 is under *Risks*. The character class and the literal
`$argon2id$` prefix hold no `{`, so a value that passes the predicate holds no `{{`.

**A stubbed run skips the assert.** A stubbed shadow run feeds the assert `stub-` strings:
R1's pinned hash facts until PR 4, then the three `_argon2` keys' ordinary secret stubs, such
as `stub-authelia-password-argon2`. Neither is PHC-shaped. The guard is R1's PR 4 precedent,
`not (homelab_secret_stubs | default(false) | bool)`. With it, R3 needs no PHC-shaped stub, so
it never edits `ansible/filter_plugins/secret_stubs.py`. R1's *Risks* gives every plugin edit
after R1's PR 5 a `k8s_unapplied` line for every declared service and a fresh operator shadow
run, and R3 avoids both. The assert renders nothing, so skipping it changes neither half of
the shadow pair. Its coverage is the predicate test, the operator-only dry run before PR 3,
and every real deploy.

PR 3 lands the assert over today's facts, `authelia_password_hash` and its two siblings. Its
operator-only pre-merge dry run is not stubbed and reads the live digests. So it proves that
the pattern, the character class and the lengths accept them before any value moves.

**The rotation procedure** goes into `docs/secret-rotation.md` and replaces the rehash flag.
Each step handles a plaintext password, so the whole procedure is **operator-only**. It runs in
a worktree on a branch, never in the primary checkout. Below, `KEY` stands for the
plaintext's SOPS name.

1. Set the new plaintext with `sops set`.
2. Mint and validate the digest in a one-shot pod, because daniel-box has no Docker. The
   plaintext travels over stdin, as the deleted tasks sent it. The flags match the hashing block
   in `config-secret.yaml.j2`. The image is the one `authelia_k8s_image` names.

   ```bash
   sops -d --extract "[\"$KEY\"]" ansible/vars/secrets.yml \
     | sudo k3s kubectl -n homelab run authelia-mint --rm -i --attach --quiet \
         --restart=Never --image="<authelia_k8s_image>" --command -- sh -c '
       IFS= read -r pw
       d=$(authelia crypto hash generate argon2 --password "$pw" --iterations 3 \
             --memory 65536 --parallelism 4 --salt-size 16 --key-size 32 \
           | grep -o "\$argon2id\$[^[:space:]]*")
       authelia crypto hash validate --password "$pw" -- "$d" \
         | grep -qx "The password matches the digest." && printf "%s\n" "$d"' \
     | grep -x '\$argon2id\$v=19\$m=65536,t=3,p=4\$[A-Za-z0-9+/]*\$[A-Za-z0-9+/]*' \
     | jq -R . \
     | sops set --value-stdin ansible/vars/secrets.yml "[\"${KEY}_argon2\"]"
   ```

   The pipeline prints nothing on success. A failed validation emits no digest, so `sops set`
   receives nothing and changes nothing.
3. If D3's `cluster_vars.py` is at the base, run `cluster_vars.py write`, which re-encrypts
   the changed digest into D3's cluster file. Once D3.2d is at the base, `write` also refuses
   a digest that fails the predicate, naming the key and never the value (*Risks*). Commit
   both SOPS values, and the cluster files when `write` changed them, in one commit, then
   open the PR.
4. Land the PR with `land.sh`, passing `--tags authelia`. A rotation PR changes only
   `ansible/vars/secrets.yml` and D3's cluster files, so land.sh derives no deploy tag from
   it. Without `--tags`, `scripts/deploy_tools/land_lib/land_tags.py:plane_note` adds
   `_ROTATION_NOTE`, which says a rotation "maps to no deploy tag by construction".
   `deploy.py:no_tag_outcome` in the same package then ends the landing
   `needs-manual-apply`, exit 1, and nothing is deployed. Authelia keeps verifying the old
   digest. With `--tags authelia`, `classify.py:classify` leaves that note unread, and
   `deploy.py:deploy_phase` deploys authelia at the merge commit through the health gate.
   If a landing already ran without `--tags`, deploy `--tags authelia` from a checkout at
   that merge commit, not from the rotation worktree, whose branch can be behind master. The
   fix-forward under *Rollback* is the one case that deploys from the worktree, before the
   merge, and it still lands with `--tags authelia` straight after.
5. A claude-agent rotation also needs the `claude_code` apply, as it does today. Run it only
   after step 4's landing reads `settled`. If the authelia deploy failed, the old digest is
   still live, and the old plaintext in the agent's home still matches it.
   `secret_rotation.py consumers authelia_agent_password` prints the apply command. The
   agent's login fails from the first of steps 4 and 5 until both have run, whichever runs
   first.
6. Log in as the rotated user.

Step 4 holds while Ansible deploys authelia. After D5 moves authelia to Flux, a merged
cluster file reaches the live Secret through Flux, as D3's exit criterion 3 shows for peanut.
D5's authelia PR rewrites step 4 in `docs/secret-rotation.md` (cross-slice note).

**Rollback of a rotation.** Revert the rotation commit, which restores both ciphertexts
together. The revert is a secrets-only PR too, so land it with `--tags authelia`, as in step
4, and repeat step 5 for a claude-agent rotation. The deploy-time assert checks the reverted
value too.

**The one-time copy in PR 4.** This step is **operator-only**. The operator runs it on
daniel-box, in PR 4's worktree. It reads each user's digest out of the live Secret and stores it
with `sops`, printing nothing:

```bash
pick='import json, sys, yaml
users = yaml.safe_load(sys.stdin)["users"]
name = sys.argv[1]
if name == "operator":
    (name,) = [u for u in users if u not in ("claude-ui", "claude-agent")]
print(json.dumps(users[name]["password"]))'
copy_digest() {  # $1: an Authelia username, or "operator"; $2: the SOPS key
  sudo k3s kubectl -n homelab get secret authelia-config -o json \
    | jq -r '.data["users_database.yml"]' | base64 -d \
    | uv run python -c "$pick" "$1" \
    | sops set --value-stdin ansible/vars/secrets.yml "[\"$2\"]"
}
copy_digest operator authelia_password_argon2
copy_digest claude-ui authelia_claude_password_argon2
copy_digest claude-agent authelia_agent_password_argon2
uv run python scripts/secrets_mgmt/secret_rotation.py sync
```

The operator then commits `ansible/vars/secrets.yml` and `ansible/secret_rotation.yml` on PR 4's
branch. D3 and R3 have no ordering edge. D3's `ansible/vars/cluster-vars.sops.env` holds every
key the cluster render references across all 56 tags, so PR 4's three new references fail
D3's CI check `cluster_vars.py check` when D3 is at PR 4's base. In that order the operator
also runs D3's `cluster_vars.py write` (**operator-only**) on PR 4's branch and commits the
cluster file and its `cluster-vars.source` with the keys. `sync` adds three registry
entries, so the `regen-doc-fragments` hook may rewrite a fragment and fail once; stage it and
commit again. The read uses `sudo k3s kubectl` because the
read-only ServiceAccount cannot read Secrets. `sops set` takes a JSON-encoded value, so
`json.dumps` quotes the digest. Nobody has run this pipeline yet. PR 4's pre-merge digest check
catches any value it stores wrongly.

The TOTP seeding task after the apply stays. It writes after the apply rather than feeding a
template, so it belongs to R4's inventory of post-apply tasks.

### The census

A test in `scripts/renderer/tests/`, in a module named `test_renderer_…` as R1 requires,
computes three sets from the tree:

- **Set by tasks:** every `set_fact` key and `register` name in every task file the k8s play
  in `ansible/deploy.yml` reaches, nested blocks included. The walk starts at the play's own
  tasks and follows `import_tasks`, `include_tasks`, `import_role` and `include_role`. So it
  covers `pre_tasks/load_secrets.yml`, `tasks/k8s_batch.yml` and the roles it includes, the
  helper roles those include (`k8s/image-builder`, `k8s/manifests`) and the post_tasks.
  `k8s_built_image_tags` is set inside `k8s/image-builder`, so finding it also proves the walk
  enters an included role.
- **Derived:** every name that R1's `task_inputs.derived_facts` returns for any of the 56 tags,
  every `include_role` var that `task_inputs.include_vars` returns, the secret keys, and the
  `containers_list` view that `context.play_context` lays down.
- **Reached:** the walk starts from the names read by the templates that the render reads.
  For each of the 56 tags, those are the files `scripts/lib/k8s_roles.py:resolved_manifest_files`
  returns, the role's deferred files, and every template those pull in through
  `lookup('template')`, `{% import %}` or `{% include %}`. That brings in
  `ansible/templates/` and the `templates/config/` files. The image contexts join them:
  image-builder's `context-configmap.yaml.j2`, and each `Dockerfile*.j2` and context template
  that an image-builder include site names in its `vars:`. A one-off `*-job.yaml.j2` that a
  role's own task applies is outside the set. So `image-builder/templates/build-job.yaml.j2`,
  which reads the task-set `image_builder_content_tag`, is outside it, and R1's out-of-render
  census records that Job. The walk then repeats until it adds nothing: for each reached name
  that is not derived, it adds the names that each definition of that name reads. Definitions
  are role `defaults/` and `vars/`, `ansible/inventory/group_vars/` and `host_vars/`,
  `include_role` `vars:` and `set_fact` values. A derived name stops the walk. The renderer
  computes it from the tree, and R1's parity tests hold that computation.

The test reads names through Jinja's AST, with `meta.find_undeclared_variables` under the
environment `scripts/lib/ansible_jinja_env.py` builds. A token grep would match text that is
not a name, such as the `k8s_rebuilt_images` in a Dockerfile comment in
`valheim/templates/Dockerfile.j2`, or the `lookup('vars')` in a comment in monitor-bridge's
`env-secret.yaml.j2`.

**The accepted lookup shapes.** The census accepts four plugins, each in one shape:

- `file` and `template` read the tree. The names in their path expressions join the walk.
- `pipe` only in the shape `make_lookup` accepts: a first argument whose leading operand is
  the literal `'base64 -w0 '`. homepage's `icons-configmap.yaml.j2` is the member it must
  accept.
- `vars` only where the census can list the names statically. It recognises the call by its
  enclosing `{% for %}`: the argument is an attribute of the loop variable, and the loop's
  iterable is one of two sources. The census adds every name the source yields to the walk.
    - The `py_table('CHECKS')` of `check_table.py`. The census parses the table with
      `py_table.py`'s parser and adds every row's `token`. Each is a secret key, so it is
      derived and stops the walk. `monitor_bridge_disk_push_token` is a named member.
    - `containers_list | scrape_jobs(...)`. The census runs `scrape_jobs` over the
      inventory's `containers_list` and adds every `replicas_var`. `terraria_k8s_replicas` is
      a named member, and the walk follows its definition in `group_vars/all.yml`.

The test fails on three things:

- a name that tasks set and the walk reaches, but that is not derived;
- a `lookup` or `query` call in a reached template or expression whose plugin is not one of
  the four, or whose plugin is `pipe` or `vars` in any other shape, because the census cannot
  name what such a call reads;
- a named member it does not find: `kuma_declarations`, which a template reads directly;
  `k8s_built_image_tags`, which templates reach only through role defaults;
  `monitor_bridge_disk_push_token` and `terraria_k8s_replicas`; homepage's `pipe` call; and
  `context-configmap.yaml.j2` in its template set. `build-job.yaml.j2` is a named
  non-member, which the template set must not contain.

A name that a role default defines and a task also sets is in the first set. So a `set_fact`
that shadows a default is caught, even though a render never finds that name undefined.

### File tree

```diff
+ ansible/roles/k8s/pi-peer-backup/files/daniel-pi-ssh-host-ed25519.pub
  ansible/roles/k8s/pi-peer-backup/tasks/main.yml      (- slurp, - set_fact)
  ansible/roles/k8s/pi-peer-backup/templates/secret.yaml.j2
  ansible/roles/k8s/pi-peer-backup/files/pull-pi-peers.sh
  ansible/roles/k8s/n8n/templates/deployment.yaml.j2           (- annotation)
  ansible/roles/k8s/n8n/templates/deployment-runners.yaml.j2   (- annotation)
  ansible/roles/k8s/image-builder/tasks/main.yml               (comment only)
  ansible/templates/checksum-annotation.yml.j2                 (comment only)
- ansible/tests/services/test_n8n_image_stamp.py
  ansible/roles/k8s/authelia/tasks/main.yml            (+ 1 guarded assert, - 13 tasks)
  ansible/roles/k8s/authelia/templates/config-secret.yaml.j2
  ansible/roles/k8s/authelia/defaults/main.yml         (- toggle, stand-ins swapped)
  ansible/vars/secrets.yml                             (+ 3 keys, operator-only)
  ansible/secret_rotation.yml                          (+ 3 keys)
  scripts/renderer/shadow_diff.py                      (- pinned facts)
  scripts/renderer/renderer_lib/task_inputs.py         (- PINNED_LIVE_FACTS and its read)
  scripts/renderer/homelab_render.py                   (- R2's pinned-fact refusal and
                                                        placeholder mode's pinned read,
                                                        when R2 has landed them)
+ scripts/renderer/tests/   (the live-read census, the stub-file key test)
```

`ansible/filter_plugins/secret_stubs.py` does not change. The three `_argon2` keys take the
ordinary stub form, and the digest assert skips a stubbed run.

## Steps, as PRs

R3 moves no object between writers, so C4 has nothing to check. Each PR is deployed by
Ansible through `land.sh`. PR 4 also changes `ansible/vars/secrets.yml`, which changes the
verdict its landing prints (PR 4). No PR edits `ansible/filter_plugins/secret_stubs.py`, so none
inherits the deferral that R1's *Risks* gives a plugin edit after R1's PR 5.
`task_inputs.py` and `shadow_diff.py` are under `scripts/`, not `ansible/`.

**R2's checks on `PINNED_LIVE_FACTS`, in either landing order.** contracts.md gives R2 and R3
no ordering edge, so either may land first. Two of [R2](r2-secret-placeholders.md)'s PRs read
the constant. R2's PR 1 adds placeholder mode, which reads the constant's values. R2's PR 2
adds the pinned-fact refusal, which derives the tags that read a pinned fact and names
`authelia`, `n8n` and `pi-peer-backup` as members. One rule covers both orders: an R3 PR that
drops a tag from that derived set retires the tag's named member when the refusal's test is at
the PR's base. An R2 PR 2 that lands mid-R3 names only the tags still reading a pinned fact at
its own base. One that lands after R3's PR 4 ships no refusal (cross-slice note).

1. **pi-peer-backup's host key** (about 90 lines). Read the key over the trusted SSH
   connection, which checks it against the operator's `known_hosts`:
   `ssh daniel-pi cat /etc/ssh/ssh_host_ed25519_key.pub`. Commit it, change the template,
   delete the two tasks and add the script branch. Remove `pi_peer_backup_known_hosts_line`
   from `PINNED_LIVE_FACTS`. R1's reader-tag test follows the constant. R1's `stubs` test
   expects four pinned facts instead of five. If R2's PR 2 is at the base, retire
   `pi-peer-backup` as a named member of its refusal. R2's PR 6 edits the same
   `secret.yaml.j2`, so whichever lands second rebases. A test in `ansible/tests/services/`,
   beside the forced-command test, runs the script's classification with a stub `ssh` on
   `PATH`. A host-key failure must yield the named message, and an rsync failure must not.
2. **n8n's annotations** (about 100 lines, mostly deleted comments and one deleted test).
   Remove both annotations and their comment blocks. Retire `test_n8n_image_stamp.py`,
   retarget the drift-gate test, rewrite the comments that name n8n's stamp, and drop
   `k8s_built_images` from `PINNED_LIVE_FACTS`. R1's `stubs` test expects three pinned facts.
   If R2's PR 2 is at the base, retire `n8n` as a named member, and retarget the refusal's
   flagged case from `n8n` and `k8s_built_images` to `authelia` and `authelia_password_hash`.
   The pod template changes, so the deploy rolls n8n and n8n-runners once. Both are
   `Recreate`, so n8n is down for the roll. Land it in a quiet window; n8n is denylisted from
   auto-deploy, so `land.sh` deploys it explicitly.
3. **authelia's digest assert** (about 100 lines). Replace "Fail if no password hash could be
   obtained" with the digest assert, over `authelia_password_hash` and its two siblings, and
   guard it on `homelab_secret_stubs` (Design). Retarget the parameter-agreement test,
   keeping D3.2d's member if D3.2d is at the base. Nothing renders differently, and
   the stub file does not change.
4. **authelia's digests from SOPS** (about 380 lines, about 260 of them deleted). This PR is
   over the contract's 200 lines. The deleted tasks, their toggle, and the tests and docs that
   name them must go together: a split leaves CI red, or leaves a doc naming a deleted
   variable. The operator's copy step comes first (Design). Then the template reads the three
   keys, the assert's subject moves to them, and the twelve remaining tasks and the toggle go.
   R1's three pinned hash facts go, which empties `PINNED_LIVE_FACTS`. Two checks lose their
   subject at this commit, and both retire here rather than in PR 5:
    - R1's reader-tag test, with its `must_find` member
      `authelia/templates/config-secret.yaml.j2` for `authelia_password_hash`. No pinned fact
      is left to retarget it to.
    - If R2's PR 2 is at the base, R2's refusal, its test pair and its sentence in
      `docs/homelab-render.md`. Its derived set is empty, so its `authelia` member and its
      flagged case fail at this commit.

   R1's `stubs` test expects no pinned facts. The three `_argon2` keys take R1's ordinary stub
   form, `stub-authelia-password-argon2` and its two siblings. Each is a valid DNS label, so
   R1's DNS-label test needs no exemption. If D3.2d is at the base, this PR keeps D3.2d's
   parameter-agreement member and adds the named-member check on the three key names
   (*Risks*). If D3.2d is not at the base, D3.2d adds that check when it lands (cross-slice
   note). R2's PR 7 edits the same `config-secret.yaml.j2`, so whichever lands second
   rebases. Authelia gates SSO for the fleet, so land it in a window.

   land.sh derives `authelia` from the role files and deploys it. Because PR 4 also changes
   `ansible/vars/secrets.yml`, `land_tags.py:plane_note` adds `_ROTATION_NOTE`, and
   `health_verdict.py:health` ends the landing `needs-manual-apply`, exit 1, with "services
   deployed, the plane above not". That verdict is expected and does not mean the deploy
   failed. `secret_rotation.py consumers` names only `authelia` for each `_argon2` name, and
   the landing deployed it. An operator who lands PR 4 with `--tags authelia` gets `settled`
   instead, because `classify.py:classify` leaves the note unread when `--tags` names the
   services.
5. **The census and the stub-file check** (about 150 lines). The census test. The deletion of
   the empty `PINNED_LIVE_FACTS`, its write in `shadow_diff.py stubs`, its read in
   `task_inputs.py` and, if R2's PR 1 is at the base, placeholder mode's read of it. The
   `stubs` test's pinned-fact clause. The stub-file key test. A rerun of the shadow pair.

## Verification

**Before merging PR 3: the assert accepts the live digests (operator-only).** The dry run
decrypts SOPS through `ansible/pre_tasks/load_secrets.yml`, so the operator runs it on
daniel-box, from PR 3's worktree:

```bash
./scripts/deploy.sh --dry-run --skip-staleness-check --tags authelia \
  > /var/tmp/r3-pr3.log 2>&1 < /dev/null
```

The dry run is not stubbed. It reads the live digests back, as every run does, and must pass
the assert. The log must show the assert as `ok`, not `skipping`.

**Before merging PRs 1 and 4: the Secret bytes must not move (operator-only).** Both change
how a secret manifest is built, not what it holds. The baseline render record compares the
bytes without printing them. `manifests_render_record` and `manifests_render_record_dir` in
`ansible/roles/k8s/manifests/defaults/main.yml` switch it on. `tasks/render_record.yml`
writes `<dir>/<manifests_service>.json` as root, with a `secret_digest` keyed by the
root-only `manifests_secret_digest_key`. The run decrypts SOPS and writes as root, so the
operator runs it on daniel-box, from a worktree at the PR's base and then at its head. For
PR 4:

```bash
mkdir -p /var/tmp/r3-base /var/tmp/r3-head
./scripts/deploy.sh --dry-run --skip-staleness-check --tags authelia \
  -e manifests_render_record=true -e manifests_render_record_dir=/var/tmp/r3-base \
  > /var/tmp/r3-base/play.log 2>&1 < /dev/null
# Repeat from a worktree at the head with /var/tmp/r3-head, then compare:
jq -r .secret_digest /var/tmp/r3-base/authelia.json /var/tmp/r3-head/authelia.json
```

PR 1 runs the same check with `--tags pi-peer-backup` and compares `pi-peer-backup.json`.
The two `secret_digest` values must be equal and non-empty. That proves the committed key and
the copied digests are exact, without printing either.

**After each deploy:**

- **pi-peer-backup.** Start a manual run (**operator-only**, a write verb):
  `kubectl -n homelab create job ppb-r3 --from=cronjob/pi-peer-backup`. It must push `up` with
  a file count. Then confirm the next 23:00 run also pushes `up`.
- **n8n.** The image the cluster runs must carry the tag the renderer computes:

  ```bash
  kubectl -n homelab get deploy n8n n8n-runners \
    -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}'
  uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r3-stubs.yml
  uv run python scripts/renderer/homelab_render.py cluster --role n8n --out /tmp/r3-n8n \
    --secret-stubs /tmp/r3-stubs.yml
  grep -h 'image:' /tmp/r3-n8n/cluster/n8n/deployment*.yaml
  ```

  Both pairs must name the same `sha-<12 hex>` tag. `probe.py health n8n` must pass.
- **authelia.** `probe.py health authelia` passes, and `uv run pytest -m ui -k grafana`
  logs in through Authelia. The homelab-ui MCP server logs in as `claude-agent`, which proves
  that user's digest. The operator confirms their own login once (**operator-only**).
  PR 4's landing reads `needs-manual-apply` unless it ran with `--tags authelia` (PR 4), so
  these checks, not that verdict, decide whether PR 4 is live.

**The exit criterion:**

```bash
uv run pytest scripts/renderer/tests
```

Then repeat R1's shadow pair with a fresh `shadow_diff.py stubs` file. `compare` must print
`56 tags, <n> files, 0 differences`.

**Red-proofs:**

- The digest assert: a predicate test in `ansible/tests/services/` renders the assert's
  expressions with ansible-core's `Templar`. A well-formed digest passes. Each of these fails:
  an empty value, a `Digest: ` prefix, a trailing newline, a plaintext, `m=1024`, and two
  users sharing one digest. The same test reads the task's `when` and requires the
  `homelab_secret_stubs` guard, and only that guard, so a `k8s_dry_run` guard, which would
  skip the operator's pre-merge dry run, fails it.
- The parameter agreement: the retargeted test's existing mutation test, applied to the
  assert's parameters and, after PR 4, to the documented mint command. Once D3.2d's
  constant joins the test, the same mutation applies to its parameters.
- The successor refusal is D3.2d's, and so are its red-proofs: D3.2d's fixtures, with the
  four additions that *Risks* asks of D3.2d. One of them, the named-member check, goes red
  when a key is renamed; R3's PR 4 adds it when D3.2d lands first.
- The live-read census, with one fixture role per case:
    - a task slurps a file into a `register`, and a `set_fact` feeds a template: flagged;
    - a registered value reaches a template only through a defaults expression: flagged;
    - a `set_fact` shadows a default that a template reads: flagged;
    - a template calls `lookup('pipe', ...)` with any command but `base64 -w0 <path>`:
      flagged;
    - a `lookup('vars', row.v)` whose loop source yields the name of a task-set fact:
      flagged;
    - a `lookup('vars', ...)` over an argument the census cannot resolve: flagged;
    - a `*-job.yaml.j2` that reads a `set_fact` name: clean, and the same line in the role's
      `deployment.yaml.j2`: flagged;
    - the same role reading only a defaults value: clean.

  The census must find its named members under *The census*, and its template set must not
  contain `build-job.yaml.j2`.
- The stub-file key test: a fixture stub writer that adds one non-secret key fails it.
- The host-key branch: the paired script test in PR 1.

## Rollback

- **PR 1.** A revert restores the slurp, which needs the Pi reachable at deploy time, as it
  did before. The Secret bytes are the same either way, so nothing rolls.
- **PR 2.** A revert restores the annotations, which rolls n8n once more.
- **PR 3.** A revert restores the empty-digest assert. Nothing renders differently.
- **PR 4.** A revert restores the read-back. The read-back renders whatever digest the live
  Secret holds: the copied digest, which the pre-merge check proved identical, or a later
  rotation's. So the render is unchanged only when the live digest is right. A wrong live
  digest is the case below. A plain `git revert` of PR 4 also reverts
  `ansible/vars/secrets.yml`, so the revert branch restores that file from master before
  its commit. The three SOPS keys then stay, unused, until removed with `sops`
  (**operator-only**), and land.sh derives `authelia` from the role files with no
  `_ROTATION_NOTE`. The revert also restores the three pinned hash facts and R1's
  reader-tag test, and R2's refusal if PR 4 deleted it. If D3's cluster file is at the
  revert's base, `cluster_vars.py check` fails the revert, because the file's key names must
  equal the referenced set. The operator runs `cluster_vars.py write` (**operator-only**) on
  the revert branch, which drops the three keys, and commits the cluster files with it.
    - **After PR 5 lands, PR 4 cannot be reverted alone.** The restored "Reuse the operator
      password hash already in the cluster" sets `authelia_password_hash` with `set_fact`,
      and `config-secret.yaml.j2` reads it. PR 5's census fails CI on exactly that name, so
      `land.sh` cannot merge the revert. A revert of PR 4 alone would also leave no pinned
      hash facts, so a stubbed shadow run would render the live digests that the read-back
      task fetches. The revert is then one PR that reverts PR 5 and PR 4 together, PR 5
      first. It restores `PINNED_LIVE_FACTS` with its three hash facts, and the census
      leaves with PR 5.
- **A wrong digest after PR 4**, from the copy or from a later rotation. While Ansible deploys
  authelia, the digest assert stops a malformed value before the apply, so the portal stays
  up. After D5, D3.2d's refusal in `cluster_vars.py write` does. D5's authelia precondition
  requires D3.2d to be merged before the flip (*Risks*). A well-formed wrong digest
  locks that user out. SSH to daniel-box, `sudo k3s kubectl` and GitHub do not pass through
  Authelia, so both recoveries below stay open.
    - **Primary: fix forward (operator-only).** Run the rotation procedure for that user with
      the unchanged plaintext: mint and validate a digest, store it with `sops set`, then
      deploy `--tags authelia` from that worktree. The GitOps deployer does not apply authelia,
      but another session's `--tags authelia` from master, or a full `deploy.yml` apply,
      renders the wrong digest again. So land the PR promptly, with `--tags authelia` as in
      the procedure's step 4, because a secrets-only PR derives no deploy tag.
    - **Fallback: revert and re-mint.** This fallback applies only while Ansible deploys
      authelia. Open the revert PR under *PR 4* above: PR 4 alone before PR 5 lands, and PR 5
      with PR 4 after. Deploy from a worktree at the revert with
      `-e authelia_k8s_rehash_passwords=true`. The reverted role carries the flag again, so
      it mints all three digests from the SOPS plaintext and rolls the pod once. A revert
      deployed without the flag renders the wrong live digest again.
    - **The worktree deploy is not the end of the fallback.** Land the revert PR through
      `land.sh` straight after it. Until the revert lands, master still renders the wrong
      SOPS digest, so another session's `--tags authelia` from master locks the user out
      again. Once the revert lands, its deploy runs without the flag. The restored read-back
      then reuses the digest that the fallback minted, so the render does not change.
- **A rotation.** The rotation rollback is under Design.

## Risks

- **A host key rotation on the Pi.** A reinstall gives the Pi a new host key, and the nightly
  run fails until the new key is committed. That was true of the old pin too, which a deploy
  refreshed. The new failure names the cause.
- **n8n's Recreate roll.** PR 2 takes n8n down for one roll, so it lands in a quiet window.
- **n8n's stale pod after PR 2.** A forced rebuild that dies before the roll leaves n8n on old
  bytes, which the annotation used to roll. The drift gate fails the next deploy, and Design
  gives the remedy. The other eight built images carry the same exposure today.
- **SSO lockout from a wrong digest.** For the copy, the pre-merge digest check proves the
  rendered Secret byte-identical. A rotation changes the bytes on purpose, so that check cannot
  cover it. Three things do: the procedure validates the digest against the plaintext before
  `sops set`, the deploy-time assert refuses a malformed value, and the rotation rollback
  restores both keys together.
- **A rotation landed without `--tags authelia`.** land.sh deploys nothing and exits 1,
  printing `_ROTATION_NOTE`. Authelia keeps the old digest, so the new plaintext fails
  wherever something reads it. `scripts/diagnostics/ui_login.py` reads the plaintext from
  SOPS when it runs, so the `claude-ui` login fails once the merge reaches the checkout it
  runs in. The new digest then ships with the next `--tags authelia` from master, whoever
  runs it. The procedure's step 4 names the flag, and its step 6 login exposes a miss.
- **SSO lockout under Flux.** After D5 moves authelia, no Ansible play runs the digest
  assert. CI holds no age key (C3), so no CI check can read a digest. The procedure's `grep -x`
  and `validate` steps still stop a bad mint. A hand `sops set` outside the procedure is
  stopped only by a refusal in `write`, because `write` copies what `secrets.yml` holds into
  the cluster file. The successor guard belongs in D3's operator-only `cluster_vars.py write`,
  because every value reaches the cluster file through it. D3's *Design* makes `check` fail CI
  when a key's `.source` hash no longer matches its `secrets.yml` ciphertext. Its *partial
  failures* list says a hand-edited cluster file fails the pre-commit hook and `check`'s MAC
  proxy.
    - **D3.2d carries the guard.** D3's step 5, "D3.2d — The successor refusals", adds two
      per-key refusals to `write`. One refuses each `_argon2` key that `write` places whose
      value fails R3's PHC pattern or equals another placed `_argon2` value. The message
      names the key and never the value. The render references the operator's key always
      and a managed user's key only when that user's flag is on, so the placed keys are the
      assert's users, and D3.2d's disabled-user fixture holds that. D3.2a's character rule
      already refuses a newline, a double quote, a backslash, a control character and `{{`.
    - **The order is enforced.** D5's authelia precondition, "The asserts' successors are
      merged first", requires D3.2d's digest refusal to be merged before the flip, not only
      the keys present. The digest refusal is therefore in place whenever Flux renders
      authelia.
    - **Four additions R3 asks of D3.2d** (cross-slice note to D3). D3.2d's fixtures are a
      `t=2` digest, a `Digest: ` prefix, trailing whitespace, a pasted plaintext and two
      equal digests. Its constant holds `m`, `t` and `p`, compared with the rendered
      `memory`, `iterations` and `parallelism`. Each addition is a few lines:
        - **An empty-value fixture.** An empty digest is the case of the "Fail if no
          password hash could be obtained" assert that PR 3 replaces. D3.2a's character
          rule passes an empty value, so only the digest refusal stops it.
        - **A trailing space, not generic whitespace, with the refusal's own message.**
          D3.2a already refuses a newline or a tab, as control characters. A fixture holding
          either goes red without D3.2d, which is the vacuous case of failure class 3. So the
          whitespace fixture is a trailing space, and each refused fixture requires the
          digest refusal's own message, not any refusal.
        - **The two lengths in the constant.** The pattern's `{22}` and `{43}` encode a
          16-byte salt and a 32-byte key. The constant holds both lengths, and the
          parameter-agreement test compares them with the rendered `salt_length` and
          `key_length`, as it already compares `--salt-size` and `--key-size`.
        - **A named-member check.** It runs D3.2d's key selection over the tree and requires
          `authelia_password_argon2`, `authelia_claude_password_argon2` and
          `authelia_agent_password_argon2` among the keys it selects. A renamed key then fails the check instead of
          leaving the refusal with nothing to check. Before R3's PR 4 the render references
          none of the three, so the check lands with whichever of D3.2d and R3's PR 4 lands
          second. D3.2d adds it when R3's PR 4 is at its base. Otherwise R3's PR 4 adds it to
          D3.2d's tests in `scripts/secrets_mgmt/tests/`.
    - **The parameters agree.** D3.2d's constant is a member of the parameter-agreement test.
      After D5 deletes the Ansible assert, the assert's member leaves the test and D3.2d's
      stays.
    - **R4 names the successor.** R4 does not cite D3.2d at `70591c307`. The cross-slice note
      asks R4 to name D3.2d as the digest assert's successor (Open question 7).
- **The stubbed shadow run never runs the digest assert.** The guard skips it there. The
  predicate test, the operator-only dry run before PR 3 and every real deploy run it. The
  predicate test fails if the guard widens to `k8s_dry_run`, which would also skip the
  operator's dry run.
- **A plaintext and its digest drift apart.** A `sops set` of a plaintext alone changes no
  login, before R3 and after it. `authelia_password` stays `source: record`, and PR 4 rewrites
  its mechanism bullet in `docs/secret-rotation.md` to name the procedure.
  `secret_rotation.py rotate` refuses any key outside the `auto` tier, which covers the other
  two plaintexts.
- **Failure class 3 (empty read as clean).** A census over no templates passes. It names
  `kuma_declarations`, `k8s_built_image_tags`, `monitor_bridge_disk_push_token`,
  `terraria_k8s_replicas`, homepage's `pipe` call and `context-configmap.yaml.j2` as members
  it must find.
- **Failure class 4 (selector drift).** A census over template names alone misses a name that
  reaches a template through defaults or through a `vars` lookup. The census walks every
  definition and resolves both `vars` sources. `k8s_built_image_tags`, which no template
  names, and the two `vars`-lookup members prove it. A census over every file in `templates/`
  would instead flag `build-job.yaml.j2`, which the render never reads, so the scope follows
  `resolved_manifest_files` and `build-job.yaml.j2` is a named non-member.
- **Failure class 8 (prose asserts facts).** The design counted two live reads, and the tree
  holds three. This slice records the third with its evidence, and Open question 1 carries the
  correction to the contract and the design.

## Tests and docs that change in the same PR

- **PR 1:**
    - `ansible/roles/k8s/pi-peer-backup/CLAUDE.md`, *Auth* ("Host key pinned at deploy over
      Ansible's own connection" becomes a committed pin) and *Alerting* (the new message).
    - The script-classification test in `ansible/tests/services/`.
    - R1's `stubs` test in `scripts/renderer/tests/` expects four pinned facts.
    - If R2's PR 2 is at the base, its refusal's test loses the named member
      `pi-peer-backup`.
- **PR 2:**
    - R1's `stubs` test expects three pinned facts.
    - If R2's PR 2 is at the base, its refusal's test loses the named member `n8n`, and its
      flagged case moves to `authelia` and `authelia_password_hash`.
    - Retire `test_n8n_image_stamp.py` in `ansible/tests/services/`, whose subject is gone.
    - `test_a_dry_run_records_the_digest_n8n_stamps_and_the_gate_skips_it` in
      `ansible/tests/deploy/test_built_image_drift_gate.py` loses its reason. Keep its second
      half, that every drift-gate task reading `k8s_built_images` skips on a dry run, under a
      name that does not mention n8n. Drop the first half, which held the record unguarded
      only for n8n's render.
    - `ansible/roles/k8s/image-builder/tasks/main.yml`: the comment on "Record the registry
      digest for the drift gate of …" names the drift gate as the only reader.
    - `ansible/templates/checksum-annotation.yml.j2`: drop n8n's `checksum/image` from the
      value-mode examples.
    - `ansible/tests/_k8s_render.py`: drop `k8s_built_images` from the examples in
      `render_role_template`'s docstring.
    - `ansible/roles/k8s/n8n/CLAUDE.md`: how a rebuild rolls the pod, with the drift gate's
      remedy.
    - `docs/k8s-manifest-cycle-record.md`, two paragraphs: the one that says n8n's annotation
      reuses the macro's value mode, and the one that says it reads the registry digest under a
      dry run.
    - `ansible/roles/k8s/image-builder/CLAUDE.md` does not mention the annotation, so it does
      not change.
- **PR 3:**
    - `ansible/tests/services/test_authelia_argon2_parameters_agree.py`: compare the assert's
      parameters with the rendered hashing block too, and add the assert's task name as a
      named member beside `HASH_TASK_PODS`. If D3.2d is at the base, its constant's member
      stays in the test.
    - The assert's predicate test in `ansible/tests/services/`, with the guard clause.
    - `ansible/roles/k8s/authelia/CLAUDE.md`, *The `claude-ui` user*: one line on the assert.
- **PR 4:**
    - `ansible/tests/services/test_authelia_claude_ui_user.py`: the overrides become the three
      `_argon2` keys, and `test_the_hash_read_back_is_keyed_by_username` retires with its
      subject.
    - `ansible/tests/services/test_authelia_argon2_parameters_agree.py`: `HASH_TASK_PODS` and
      the task-command parse go. The mint command in `docs/secret-rotation.md` becomes the
      second named member. If D3.2d is at the base, its constant's member stays. If not,
      D3.2d adds it when it lands.
    - If D3.2d is at the base, its tests in `scripts/secrets_mgmt/tests/` gain the
      named-member check on the three key names (*Risks*), which the render first satisfies
      at this commit. If not, D3.2d adds the check when it lands (cross-slice note to D3).
    - `ansible/tests/k8s/test_k8s_manifests_routes.py`: `_k8s_authelia_config` passes
      `authelia_password_argon2` instead of `authelia_password_hash`. Every other repo render
      resolves an unsupplied secret name through `StubUndefined`, as it resolves the hash
      facts today, so it needs no seed.
    - R1's secret-key census in `ansible/tests/k8s/`: the hashing-task exemptions go. Its named
      member becomes crowdsec's "Register the remote agent machines on the LAPI", which names
      `crowdsec_k8s_agent_password` and is guarded by `not k8s_no_mutate`.
    - R1's out-of-render census: drop authelia's three `kubectl run` hashing tasks from its
      pinned frozenset.
    - R1's reader-tag test for `PINNED_LIVE_FACTS` retires, with its `must_find` member
      `authelia/templates/config-secret.yaml.j2` for `authelia_password_hash`. R1's `stubs`
      test expects no pinned facts.
    - If R2's PR 2 is at the base: R2's pinned-fact refusal, its test pair (`_is_flagged`
      and `_is_clean`) and its sentence in `docs/homelab-render.md` go.
    - `ansible/filter_plugins/secret_stubs.py` does not change. R1's DNS-label test covers
      the three new keys unchanged, because their ordinary stubs are valid labels.
    - `ansible/roles/k8s/authelia/CLAUDE.md`, *The `claude-ui` user*: the rehash bullet
      becomes a pointer to the procedure. The `vanished-identifier` lint flags a section that
      still names the deleted toggle, so this edit is required.
    - `ansible/roles/k8s/authelia/defaults/main.yml`: the toggle's comment goes with it.
    - `docs/authelia-sessions-and-crowdsec-init.md`: the paragraph "Rotating either password
      needs `-e authelia_k8s_rehash_passwords=true`", and the last sentence of "The hashing
      parameters are configured twice", which says the `authelia crypto hash generate argon2`
      task mints the digests.
    - `docs/claude-agent-user.md`: the rotation sentence points at the procedure.
    - `docs/secret-rotation.md`: the procedure, and the `authelia_password` record bullet.
      The procedure carries step 4's `land.sh --tags authelia` with its reason, that a
      secrets-only PR derives no deploy tag, and step 5's order for the `claude_code`
      apply. D5's authelia PR rewrites step 4 for Flux (cross-slice note).
    - `ansible/secret_rotation.yml`: three keys at tier `assisted`.
    - If D3's `cluster_vars.py` is at the base: `ansible/vars/cluster-vars.sops.env` and its
      `cluster-vars.source`, which the operator's `cluster_vars.py write` regenerates
      (Design).
    - `scripts/diagnostics/tests/test_probe_releases_check_mode.py` quotes authelia tasks as
      inline fixture strings, not the real file, so it does not change.
- **PR 5:**
    - R1's `stubs` test drops its pinned-fact clause, and the stub-file key test replaces it.
    - If R2's PR 1 is at the base, placeholder mode's read of the constant goes, with any R2
      test that names a pinned value.
    - `docs/homelab-render.md` states that the renderer pins nothing.

## Complexity delta

- **Added:** one public key file; one script branch with its test; three SOPS keys; one
  deploy-time digest assert, guarded on `homelab_secret_stubs`, with its predicate test; one
  documented rotation procedure; one census test with two `vars` resolvers; one stub-file key
  test; and, when D3.2d lands before PR 4, the named-member check on the three key names,
  which D3.2d adds otherwise.
- **Removed:**
    - two Pi-reading tasks;
    - two annotations and their test;
    - thirteen authelia tasks: the claude-agent plaintext assert, two read-back tasks, three
      reuses, three mint pods, three extracts and the empty-digest assert;
    - the `authelia_k8s_rehash_passwords` toggle;
    - `PINNED_LIVE_FACTS`, the code that writes and reads it, and R1's reader-tag test;
    - R2's pinned-fact refusal and its test pair, when R2's PR 2 has landed first.
- **Net:** authelia's deploy starts no pod, pi-peer-backup's render does not need the Pi up,
  and the renderer pins nothing.

## Open questions

1. **Does the contract's slice name change?** contracts.md row R3 says "Remove the two
   pre-apply live reads". design.md's slice-index row says "pi-peer-backup and n8n render
   without reading live state". That row is design.md's only mention of R3's scope; it has no
   render table. *Resolution:* applied. contracts.md row R3 reads "Remove the pre-apply live
   reads", design.md's row names authelia, and this file's title follows the contract row.
1. **Who owns authelia's digest read, R3 or D5?** contracts.md row R3 does not say, and
   `deploy/d5-bulk-move.md` Open question 4 converts it "unless R3 takes it". *Resolution:* R3
   takes it, in PR 4, by copying the live digests rather than minting new ones. D5 has
   dropped its own argon2 row and question, and its task-table row and authelia precondition
   generate no keys. One edit is still pending: D5's Baseline bullet "Pre-apply live reads"
   says "R3 PR 3 adds the SOPS keys" and deletes the read, reuse and hash tasks. In R3, PR 3
   adds only the assert, and PR 4 adds the keys and deletes the tasks. The bullet should name
   R3's PR 4 (cross-slice note). D5's authelia precondition requires the three keys in D3's
   cluster file, so the stale PR number cannot start an early flip. *Owner:* D5.
1. **Which R1 symbol holds the pinned facts?** *Resolution:* closed. R1 names
   `PINNED_LIVE_FACTS` in `scripts/renderer/renderer_lib/task_inputs.py`. R3's PRs 1, 2 and 4
   remove its entries, and PR 5 deletes it. The census needs no exported list from R1,
   because it calls `task_inputs.derived_facts` and `task_inputs.include_vars` for every tag.
1. **Does `authelia crypto hash validate` print exactly "The password matches the digest."?**
   The procedure greps its output rather than trusting its exit status. *Resolution:* PR 4
   runs `validate` once in the pinned image against a throwaway password, once matching and
   once not, and writes the observed strings into the procedure.
1. **Does `sops set --value-stdin` JSON-decode its input?** `sops set --help` (3.13.3) says
   the value "must be a JSON encoded string", and that the argument is omitted with
   `--value-stdin`. It does not say whether stdin is decoded. *Resolution:* both pipelines
   JSON-encode. The operator's first run, on PR 4's branch, settles it, and PR 4's digest
   check catches a wrong value.
1. **Should the nightly pin check also run on a schedule of its own?** The nightly run checks
   the pin every night already. *Resolution:* no new check. Revisit when M-area moves the
   push monitors to rules.
1. **What guards a stored digest once D5 moves authelia?** The deploy-time assert is an
   Ansible task, and CI cannot decrypt. *Resolution:* D3.2d, D3's step "The successor
   refusals", adds R3's PHC-pattern and distinctness refusal to the operator-only
   `cluster_vars.py write`. D5's authelia precondition, "The asserts' successors are merged
   first", requires it merged before the flip. Two parts stay open, each a cross-slice note:
    - D3.2d takes the four additions under *Risks*: an empty-value fixture, a trailing-space
      fixture with every refused fixture requiring the digest refusal's own message, the two
      lengths in its constant, and the named-member check on the three key names when R3's
      PR 4 is at its base. R3's PR 4 adds that check when D3.2d is at its base, so the check
      exists in either landing order. *Owner:* D3, before D5's authelia PR merges.
    - R4 names D3.2d as the digest assert's successor. *Owner:* R4.
1. **Does R2 accept that the refusal goes in R3's PR 4?** R2's Open question 11 says the R3 PR
   that deletes the constant deletes the refusal. The refusal's members fail when the constant
   empties, which is PR 4, one PR before the deletion. *Resolution:* R2 rewords its Open
   question to "the R3 PR that empties the constant" (cross-slice note). *Owner:* R2.

## Review log

### Round 1 — per-slice adversarial review

Findings raised:

- R3-1 (high): PR 3's recovery from a wrong digest does not work, because the reverted
  read-back reuses the bad digest.
- R3-2 (high): the live-read census reads only names that templates use directly.
- R3-3 (high): PR 3 deletes every check on the digest and adds a rotation path with no check.
- R3-4 (medium): existing tests that PR 2 and PR 3 break are not listed, R1's named member
  included.
- R3-5 (medium): docs that describe the rehash flag and the n8n annotation are left stale.
- R3-6 (medium): R3 takes authelia's digest read, which D5 also plans; contracts.md and D5 are
  not reconciled.
- R3-7 (medium): "covers the incident by construction" leaves out the forced-rebuild case.
- R3-8 (medium): R3 depends on R1 symbols R1 never defines, and contradicts itself about the
  pinned list.
- R3-9 (medium): the operator SOPS step names no worktree and no command that keeps the digest
  out of the terminal.

Fixed:

- **R3-1.** *Rollback* gives fix-forward through the rotation procedure as the primary
  recovery, and revert plus `-e authelia_k8s_rehash_passwords=true` as the fallback. It states
  that SSH, `sudo k3s kubectl` and GitHub stay reachable during a lockout. The PR 4 bullet now
  says the revert renders whatever digest is live. Confirmed in
  `ansible/roles/k8s/authelia/tasks/main.yml`: "Reuse the operator password hash already in the
  cluster" reuses any non-empty live digest unless the toggle is set.
- **R3-2.** *The census* walks names through defaults, inventory vars, `include_role` `vars:`
  and `set_fact` values until it adds nothing, reads names through the Jinja AST, includes
  `Dockerfile*.j2`, and refuses a non-`file`/`template` lookup. `k8s_built_image_tags` is a
  second named member, with a defaults-only red fixture. Confirmed: `git grep -c
  k8s_built_image_tags` finds it in nine `defaults/main.yml` files and no template.
- **R3-3.** PR 3 adds a deploy-time assert on the PHC form and on distinct digests.
  *Design* gives the mint command, a `validate` step and a rotation rollback, and *Risks*
  covers rotations. The two plaintext asserts are retargeted or deleted.
- **R3-4.** Every listed file is in *Tests and docs*, with what happens to it, including R1's
  two censuses and a replacement named member. PR 3 split into an assert PR and a switch PR,
  and the switch PR's size is recomputed with the reason it exceeds 200 lines.
- **R3-5.** All the authelia docs and n8n comments are listed.
  `ansible/roles/k8s/image-builder/CLAUDE.md` does not change: its only n8n mentions are the
  caller list and the #2813 history, and it does not describe the annotation.
- **R3-6.** Open question 2 names contracts.md row R3 and D5's Open question 4, records that
  R3 copies rather than mints, and sends the D5, contracts and design edits as cross-slice
  notes. Open question 1 corrects the "render table" description.
- **R3-7.** *Design* states the forced-rebuild case. It names the drift gate as what catches
  it, gives an operator-only remedy for when the forced rebuild reproduces the digest, and
  argues why D5.4's push-once rule makes the case moot under Flux. The content tag postdates
  the incident (`70dfd2387`, 2026-09-22), which is now in the Baseline.
- **R3-8.** R3 no longer names `PINNED_LIVE_FACTS`. The end state is deletion, with a test that
  the stub file holds only secret stubs and the sentinel. The exit criterion, red-proof, file
  tree and complexity delta agree, and Open question 3 asks R1 to name the constant.
- **R3-9.** *Design* gives the operator-only copy pipeline. It runs in PR 4's worktree, never
  the primary checkout, and commits both files on the PR branch. It prints no value.

Refuted: none.

### Round c1 — per-slice adversarial review

Findings raised:

- R3-c1-01 (high): the live-read census fails at baseline, because templates call the `pipe`
  and `vars` lookups, and the Baseline claim that they do not is false.
- R3-c1-02 (high): R3 drops the `PINNED_LIVE_FACTS` obligations that R1 and R2 assign to it,
  so PRs 1, 2 and 4 break named-member tests.
- R3-c1-03 (high): PR 4's stub edit names a module that does not exist and leaves out the
  fleet-wide landing that R1 makes R3 state.
- R3-c1-04 (medium): the census template scope includes out-of-render job templates that
  legitimately read task-set facts.
- R3-c1-05 (medium): the pre-merge dry run and the render-record check decrypt SOPS but are
  not marked operator-only (C8).
- R3-c1-06 (medium): no successor for the deploy-time digest assert once D5 moves authelia
  to Flux.

Fixed:

- **R3-c1-01.** Confirmed: the grep at `70591c307` prints 35 `file`, 21 `template`, one
  `ansible.builtin.template`, one `pipe` and four `vars`, one of them comment text in
  monitor-bridge's `env-secret.yaml.j2`. The Baseline records each call site.
  `scripts/lib/k8s_yaml.py:make_lookup` implements all four plugins and accepts `pipe` only
  as `base64 -w0 <path>`. *The census* accepts that `pipe` shape alone. It resolves the two
  `vars` sources statically, through `check_table.py`'s `CHECKS` and through `scrape_jobs`
  over `containers_list`, and refuses any other `vars` call. `monitor_bridge_disk_push_token`,
  `terraria_k8s_replicas` and homepage's `pipe` call are named members. Two new red fixtures
  cover a `vars` lookup naming a task-set fact and one the census cannot resolve.
- **R3-c1-02.** Confirmed in R1 (`PINNED_LIVE_FACTS` in `renderer_lib/task_inputs.py`, the
  reader-tag test and its `must_find` member) and in R2 (*Pinned live-read facts*, Open
  question 11). Open question 3 is closed with the symbol. *Steps* states one rule for both
  landing orders, per R2 PR. PRs 1 and 2 retire R2's `pi-peer-backup` and `n8n` members when
  R2's PR 2 is at the base. PR 2 retargets the flagged case to `authelia`. Each PR updates
  R1's `stubs` count. One departure from the finding: R2's refusal goes in PR 4, not PR 5.
  PR 4 empties the constant, so the refusal's derived set is empty and its `authelia` member
  and flagged case fail at that commit. R1's reader-tag test retires in PR 4 for the same
  reason. Open question 8 asks R2 to reword its Open question 11 to match.
- **R3-c1-03.** Fixed by a third route, which removes the plugin edit. The digest assert takes
  the `homelab_secret_stubs` guard, after R1's PR 4 precedent for sonarr's reads and asserts.
  A stubbed run then never needs a PHC-shaped stub, so R3 never edits
  `ansible/filter_plugins/secret_stubs.py` and inherits no fleet-wide deferral. The wrong
  `renderer_lib/secret_stubs.py` path is gone from the file tree and *Tests and docs*. PR 3
  no longer changes the pinned stub values. The finding's DNS-label exemption is not needed:
  the three `_argon2` keys take the ordinary form `stub-authelia-password-argon2` and its
  siblings, which are valid labels. The predicate test requires the guard and fails on a
  `k8s_dry_run` guard, which would skip the operator's dry run.
- **R3-c1-04.** Confirmed: `image-builder/templates/build-job.yaml.j2` reads the task-set
  `image_builder_content_tag`, and `k8s_roles.py:resolved_manifest_files` excludes every
  `-job.yaml` (`JOB_MANIFEST_SUFFIX`). The census scope is the render's own file list, the
  templates it pulls in, and the image contexts. `build-job.yaml.j2` is a named non-member,
  and a fixture pairs a clean `*-job.yaml.j2` with a flagged deployment template.
- **R3-c1-05.** Both steps are **operator-only**. The render-record step gives the full
  command with `manifests_render_record` and `manifests_render_record_dir` and the `jq -r
  .secret_digest` comparison. It cites the baseline mechanism in
  `ansible/roles/k8s/manifests/defaults/main.yml` and `tasks/render_record.yml`, not R2. The
  n8n check also passes `--secret-stubs`, which R1's `cluster` command takes.
- **R3-c1-06.** *Risks* records the lockout under Flux. Open question 7 names D3's
  operator-only `cluster_vars.py write` as the successor guard, which already refuses values
  per key. Cross-slice notes go to D3, D5 and R4. *Rollback* says which guard applies before
  and after D5.

Also changed: the title follows contracts.md row R3, which already reads "Remove the
pre-apply live reads", and Open question 1 is marked applied. D3's *Design* says
`cluster-vars.sops.env` holds the keys referenced "across all 56 tags and not only the
flux-owned ones". So when D3 lands before PR 4, PR 4 and each later rotation run D3's
operator-only `cluster_vars.py write` and commit the cluster files.

Refuted: none. The R3-c1-03 exemption request is moot rather than refuted, because no
PHC-shaped stub exists.

### Round integration-2 — integration review

Findings raised:

- X2-03 (high): D3's `cluster_vars.py write` lacks the successor refusals that R2 and R3 rely
  on once Flux renders authelia and mosquitto.

Fixed:

- **X2-03, R3's part.** Confirmed in the plan text. D3's *Design* bullet for `write` refuses
  only "a newline, a double quote, a backslash or a control character", in any referenced
  value, and names no `_argon2` key. D5's authelia precondition reads "D3's cluster file holds
  R3's three argon2 keys", which is presence alone. R3's *Risks* said `write` "already
  refuses some characters per key", which was wrong on both counts. Changes:
    - *Design* names the digest predicate and shows from `config-secret.yaml.j2` why the
      referenced `_argon2` keys are exactly the assert's users: the operator's `password:`
      line is unconditional, and the other two sit inside the `manage_*` gates, which PR 4
      keeps. It records that the predicate excludes `{`, so R3 needs no `{{` refusal.
    - *Risks* states that `write` does not carry the guard yet. It specifies what D3 adds:
      the predicate and distinctness refusal, a message naming the key, one fixture per
      predicate-test case, and a named-member check on the three key names that lands with
      whichever of D3's refusal PR and R3's PR 4 comes second. D3's fixtures use a trailing
      space instead of a trailing newline and require the digest refusal's own message,
      because `write`'s character rule already refuses a newline. D3's parameters join the
      parameter-agreement test. D5 requires the refusal merged, and R4 cites it.
    - The rotation procedure's step 3, *Rollback*'s "after D5" sentence, and Open question 7
      match.
    - *Steps* and *Tests and docs* for PR 3 and PR 4 keep D3's member in the
      parameter-agreement test in either landing order. *Verification* names D3's
      red-proofs.
  The code belongs to D3, D5 and R4, and the `{{`, C3 and `mqtt_password_hash` parts belong
  to R2's successors. Those go as cross-slice notes. R3 adds no code, so *Complexity delta*
  does not change.

Refuted: none.

### Round i1 — per-slice adversarial review

Findings raised:

- R3-i1-01 (high): the X2-03 fix disagrees with D3.2d about what D3 owns, so in one landing
  order nothing owns the named-member check.
- R3-i1-02 (medium): PR 4's rollback after PR 5 lands is unspecified, and a revert of PR 4
  alone cannot land.
- R3-i1-03 (medium): D5 still says R3's PR 3 adds the argon2 keys and deletes the read-back;
  R3 does both in PR 4.

Fixed:

- **R3-i1-01.** Confirmed in the plan text. D3.2d's fixtures are `t=2`, a `Digest: ` prefix,
  trailing whitespace, a plaintext and two equal digests. It has no empty case and no
  named-member check, and its constant holds `m`, `t` and `p` only. D3's `write` design
  already refuses `{{` in D3.2a and `:` in `mqtt_password_hash` in D3.2d, and D5's authelia
  precondition already requires D3.2d merged. R3 had both as pending requests. Changes:
    - *Risks* cites D3.2d by name, states that it carries the guard and that D5's
      precondition enforces the order, and drops the stale "does not yet carry the guard"
      bullet.
    - *Risks* lists four additions R3 asks of D3.2d, each with its reason, in a cross-slice
      note to D3: an empty-value fixture, a trailing-space fixture with each refused fixture
      requiring the digest refusal's own message, the two lengths in its constant, and the
      named-member check when R3's PR 4 is at its base. R3's PR 4 adds the check when D3.2d
      is at its base, so the check exists in either order.
    - The rotation procedure's step 3, *Steps* for PRs 3 and 4, *Verification*, *Rollback*,
      *Tests and docs* and Open question 7 name D3.2d. *Complexity delta* counts the check
      that PR 4 may add.
    - `test_authelia_argon2_parameters_agree.py` already compares `--salt-size` and
      `--key-size` with `salt_length` and `key_length`, so the lengths addition reuses that
      mapping.
- **R3-i1-02.** Confirmed. In `ansible/roles/k8s/authelia/tasks/main.yml`, "Reuse the
  operator password hash already in the cluster" sets `authelia_password_hash`, which
  `config-secret.yaml.j2` reads. After PR 5, the census fails CI on that name. *Rollback*
  makes the revert after PR 5 one PR that reverts PR 5 and PR 4 together, which also
  restores `PINNED_LIVE_FACTS`. The fallback names that revert, applies only while Ansible
  deploys authelia, and must be followed by landing the revert through `land.sh`. A revert
  of PR 4 with D3's cluster file at its base also needs the operator's `cluster_vars.py
  write`, because D3's `check` requires the dotenv's key names to equal the referenced set.
- **R3-i1-03.** Confirmed: D5's Baseline bullet "Pre-apply live reads" says "R3 PR 3 adds
  the SOPS keys". Open question 2 records the pending edit with D5 as owner, corrects the
  bullet's name, and re-sends the cross-slice note.

Refuted: none.

### Round f0 — per-slice adversarial review

Findings raised:

- R3-i2-01 (high): the rotation procedure's step 3 says `land.sh` deploys authelia, but
  land.sh maps a change to `ansible/vars/secrets.yml` alone to no deploy tag.

Fixed:

- **R3-i2-01.** Confirmed at `70591c307`. In `scripts/deploy_tools/land_lib/`,
  `land_tags.py:plane_note` appends `_ROTATION_NOTE` on `cs.secrets` and adds no tag.
  `deploy.py:no_tag_outcome` then ends a landing with no tag and a non-empty plane
  `needs-manual-apply`, exit 1, without deploying. The step's reason was wrong too:
  `k8s_autodeploy: false` in authelia's defaults governs only the GitOps deployer's
  image-pin arm, not what land.sh derives. Changes:
    - The procedure's step 3 now only commits and opens the PR. A new step 4 lands it with
      `--tags authelia`, which `classify.py:classify` honours by leaving the plane unread,
      and which `deploy.py:deploy_phase` deploys at the merge commit. A `--tags` landing
      still runs `ci.py:preflight`. Its check,
      `scripts/deploy_tools/deploy_lib/tags.py:_cmd_blockers`, blocks only on a
      `_BROAD_MANUAL_PREFIXES` path, so a `secrets.yml` change does not block it. The step
      names the recovery when a landing already ran without the flag: deploy from a
      checkout at the merge commit, not from the rotation worktree. The fix-forward under
      *Rollback* stays the one pre-merge deploy from a worktree.
    - A new step 5 runs the `claude_code` apply only after step 4 reads `settled`, takes the
      command from `secret_rotation.py consumers`, and states that the agent's login fails
      between the two steps in either order.
    - The rotation rollback, the fix-forward recovery and *Tests and docs* for
      `docs/secret-rotation.md` carry the same landing. *Risks* records a rotation landed
      without the flag, including the `claude-ui` login that `ui_login.py` breaks by reading
      the new plaintext. A cross-slice note asks D5's authelia PR to rewrite step 4 for Flux.

Also changed:

- PR 4 adds three keys to `secrets.yml` and changes authelia's role files, so land.sh
  derives `authelia`, deploys it, and `health_verdict.py:health` still ends the landing
  `needs-manual-apply` on `_ROTATION_NOTE`. `classify.py:_owed_to_a_hand` passes
  `secrets.yml` to `plane_note`, because
  `scripts/deploy_tools/shared_role_reach.py:paths_a_hand_must_apply` drops only the paths of
  a shared role that moves no rendered manifest. *Steps* (the opening paragraph and PR 4) and *Verification* state the expected
  verdict. The PR 4 rollback now says that a plain `git revert` also reverts `secrets.yml`,
  so the revert branch restores that file from master. That keeps the three keys, as the
  bullet already said, and its landing fires no rotation note.
- The procedure's lead-in names `KEY` as the plaintext's SOPS name.

Refuted: none.
