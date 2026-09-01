# scripts/

One file per topic. Home Assistant merges every `*.yaml` here into a single `script:`
mapping through `!include_dir_merge_named scripts/` in `configuration.yaml`, so a file is a
plain YAML mapping of `script_name:` entries and nothing else. A script name may appear in
only one file: HA would let a later file silently override an earlier one, and
`validate_ha_config.py` refuses that instead.

Managed by Ansible. The init container copies these files into `/config/scripts/` on every
pod start and sweeps the directory first, so git is the source of truth: a file removed here
disappears from the pod, and an edit made in the HA UI does not survive a deploy. The UI
shows these scripts as read-only, because a directory include leaves the script editor no
file to write to.

To change one, edit the file and run:

```
./scripts/deploy.sh --tags home-assistant
```

To add a file, also add its name to `home_assistant_script_files` in `defaults/main.yml`.
That list is what the ConfigMap and the init container ship, and `validate_ha_config.py`
fails when the list and this directory disagree.

The authoring conventions (copy-not-template, math in a tested macro, the single-writer
rules in `state/sanctioned_writers.yml`) are in the role's `CLAUDE.md` and the
`ha-edit-automation` skill.
