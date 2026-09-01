# automations/

One file per topic. Home Assistant merges every `*.yaml` here into a single `automation:`
list through `!include_dir_merge_list automations/` in `configuration.yaml`, so a file is a
plain YAML list of automations and nothing else.

Managed by Ansible. The init container copies these files into `/config/automations/` on
every pod start and sweeps the directory first, so git is the source of truth: a file
removed here disappears from the pod, and an edit made in the HA UI does not survive a
deploy. The UI shows these automations as read-only, because a directory include leaves the
automation editor no file to write to.

To change one, edit the file and run:

```
./scripts/deploy.sh --tags home-assistant
```

To add a file, also add its name to `home_assistant_automation_files` in
`defaults/main.yml`. That list is what the ConfigMap and the init container ship, and
`validate_ha_config.py` fails when the list and this directory disagree, so a file that
would validate clean and never reach the pod cannot land.

`probe.py ha verify-automations` reads every `id:` here and checks it loaded live. The
authoring conventions (copy-not-template, math in a tested macro, the state model) are in
the role's `CLAUDE.md` and the `ha-edit-automation` skill.
