# Pi-hole DNS continuity record

Two Pi-hole instances exist so a deploy never takes LAN DNS down.
`k8s/roles/pihole/CLAUDE.md` keeps the operating rules, within the inject hook's
character budget, and a session reads this page when it edits that role.

**The two instances roll in sequence, so one always serves DNS.** On 2026-09-28 (#2884) an
image-pin bump changed both pod templates in the same second. The controller Recreate-cycled
both instances, LAN DNS was down for 52s, and both new pods failed their image pull against the
resolver they had just replaced. Instance 1 therefore renders into the `pihole` Kustomization
and instance 2 into `pihole-deferred`. The renderer makes `pihole-deferred` depend on `pihole`
(`scripts/renderer/renderer_lib/ordering.py`), and `wait: true` holds that dependency until
instance 1 is Ready. Instance 2's pod template changes only after instance 1 serves.

The two sections below record how the roll was sequenced by hand before Flux applied pihole.
The tasks they name are deleted.

## Where the roll expectation for pihole-2 comes from

- **HISTORY — `probe.py health pihole` held a roll expectation for pihole-2 only because
  `roll_one.yml` wrote one.** The release record is stamped inside the shared include, before
  `tasks/apply_instance_2.yml` runs, so it cannot know whether that apply rolled instance 2 by
  changing its pod template (stamping no `restartedAt`) or `roll_one.yml` restarted it (stamping
  one). `rolled_by_role: true` on the `manifests_self_rollouts` entry records `restart: false`
  and hands the decision over; `roll_one.yml` then raises it through the shared
  `tasks/rollout_amend.yml`, but only where it actually issued a `rollout restart` (#2902).
  Deploy time is covered either way and more strictly: `roll_one.yml` blocks on pihole-2's
  `rollout status`, and `Verify both Pi-hole instances have a ready DNS endpoint` refuses fewer
  than two ready endpoints.
  `rolled_by_role` and `tasks/rollout_amend.yml` were retired after the flip (#5819).

## Why each private restart waits for its own apply

- **HISTORY — Each private restart needed the render AND the `changed` of the matching apply
  (#3127, mirroring #3115).** `manifests_render is changed` compares rendered bytes, so a YAML-comment
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
