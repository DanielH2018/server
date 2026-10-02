# Pi-hole DNS continuity record

Two Pi-hole instances exist so a deploy never takes LAN DNS down. The sequencing that holds
them apart, and the restart triggers each instance reads, are worked out here.
`ansible/roles/k8s/pihole/CLAUDE.md` keeps the operating rules, within the inject hook's
character budget, and a session reads this page when it edits that role.

## Where the roll expectation for pihole-2 comes from

- **`probe.py health pihole` holds a roll expectation for pihole-2 only because `roll_one.yml`
  writes one.** The release record is stamped inside the shared include, before
  `tasks/apply_instance_2.yml` runs, so it cannot know whether that apply rolled instance 2 by
  changing its pod template (stamping no `restartedAt`) or `roll_one.yml` restarted it (stamping
  one). `rolled_by_role: true` on the `manifests_self_rollouts` entry records `restart: false`
  and hands the decision over; `roll_one.yml` then raises it through the shared
  `tasks/rollout_amend.yml`, but only where it actually issued a `rollout restart` (#2902).
  Deploy time is covered either way and more strictly: `roll_one.yml` blocks on pihole-2's
  `rollout status`, and `Verify both Pi-hole instances have a ready DNS endpoint` refuses fewer
  than two ready endpoints.

## Why each private restart waits for its own apply

- **Each private restart needs the render AND the `changed` of the matching apply (#3127,
  mirroring #3115).** `manifests_render is changed` compares rendered bytes, so a YAML-comment
  edit to a template used to roll the LAN resolvers while `kubectl apply` printed every object
  `unchanged`. The restart task in `roll_one.yml` now fires on `manifests_render is changed and
  manifests_apply is changed` for the shared files (both instances mount the ConfigMap), and for
  `pihole-2` also on `manifests_deferred_render is changed` and its own
  `pihole_k8s_instance_2_apply` being changed. `tasks/apply_instance_2.yml` pins the
  `changed_when` of that apply false under `k8s_dry_run`, as the shared apply does. The secret and image
  triggers are unchanged: `verify_secret_keys.yml` can patch a Secret after an apply that printed
  `unchanged`. The include in `main.yml` still fires `pihole-2` on the deferred bytes alone,
  because it must run the apply to learn the verdict; only the restart waits for it. The #2884
  ordering holds: the apply is still reached only after instance 1 is proven serving, so the two
  pod templates never change in one request. The `pihole-2` record raise (#2902) is unchanged and
  stays consistent, since it reads whether the restart task ran.
