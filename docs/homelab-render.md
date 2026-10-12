# The homelab-render shadow renderer

`homelab-render` renders the k8s manifests from the git tree alone, as a shadow of the
Ansible manifests role. Target slice R1 builds it and proves it byte-identical to Ansible for
the same stub inputs. Ansible stays the only writer to the cluster: the renderer writes only
to the directory it is given.

The plan is [R1, shadow render with zero diff](target-architecture/render/r1-shadow-render.md).
C1 in [the contracts](target-architecture/contracts.md) defines the interface. R1 lands in
steps, and this page describes each piece once its PR has merged.

## What is in place

- `ansible/filter_plugins/secret_stubs.py` holds the stub form. It is the one definition
  that R1's renderer and its playbook checks are built to share, so neither restates it.
- `scripts/renderer/shadow_diff.py stubs` writes the stub file both halves of the shadow
  render take.
- `scripts/renderer/renderer_lib/task_inputs.py` holds `PINNED_LIVE_FACTS`, the stand-ins for
  facts a role task reads from live state.

The entry point `homelab_render.py` and its `cluster` mode follow in the next R1 PR.

## The stub form

A shadow render never reads a secret. Every key of `ansible/vars/secrets.yml` renders as its
stub instead, which the filter `homelab_secret_stub` builds:

- A key `K` becomes `stub-` plus `K` with underscores turned to hyphens, such as
  `stub-domain`. That shape is a valid DNS label, so a stubbed host name still passes
  `kubectl apply --dry-run=server`.
- `google_assistant_service_account` is the one key a template parses. home-assistant's
  `templates/config/secrets.yaml.j2` runs it through `from_json` and reads `project_id`,
  `client_email` and `private_key`. Its stub is a JSON object holding exactly those three
  fields, each valued `stub-google-assistant-service-account-<field>`, serialised with
  sorted keys.
- `become_password` never takes a stub. It is the one name in `NEVER_STUB`, because
  `ansible/pre_tasks/load_secrets.yml` hands it to privilege escalation and no template
  reads it.

Two more filters serve the checks that a later R1 PR adds to the playbooks.
`homelab_stub_keys` lists the key names that take a stub, from the parsed `secrets.yml`. It
reads keys only, so the ciphertext file is enough. `homelab_unstubbed_keys` returns the keys
whose value is not exactly its stub. It returns names only, never a value.

## Pinned live-read facts

Five facts come from live state in a real run, so the renderer cannot compute them from the
tree. Target slice R3 removes those reads. Until then, each one takes a fixed stand-in in
both halves:

- `authelia_password_hash`, `authelia_claude_password_hash` and
  `authelia_agent_password_hash`, read by the `authelia` templates.
- `pi_peer_backup_known_hosts_line`, read by the `pi-peer-backup` templates.
- `k8s_built_images`, read by the `n8n` templates. It pins to `[]`, which n8n renders as
  `checksum/image: "unstaged"`.

A string stand-in carries the secret stubs' marker, such as `stub-authelia-password-hash`.
Each entry also names its reader tags. A test in `scripts/renderer/tests/` holds those tags
equal to a word grep of the templates, so a new reader fails the suite.

## Stub output is never published

A stub render is a comparison input. Nothing may push it as the cluster artifact, because
`domain` is a secret key too: a stubbed route matches `*.stub-domain` and answers no request.
The plan's section *Stub output is never published* names the guards later slices add.

## Write the stub file

The file is a function of the git tree alone, so an agent, CI and the operator write the
same bytes for one commit. It reads only the key names of `ansible/vars/secrets.yml`, which
SOPS leaves in plaintext, and decrypts nothing.

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
```

The file holds every stub, the sentinel `homelab_secret_stubs: true` and the five pinned
facts. Pass it only to a k8s dry run or to the renderer, never to a run that applies
anything.

A stub file goes stale when a secret key is added or removed, so write a fresh one for each
commit you render.
