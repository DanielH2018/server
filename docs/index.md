# Homelab docs

Reference and runbooks for a three-host homelab: `daniel-box` (k3s server), `daniel-server`
(k3s agent), and `daniel-pi` (the one remaining Docker host).

## Where to start

- **Reference** pages are generated from the Ansible tree. They carry the timestamp and commit
  their content was last built from, and a cron regenerates them. Do not edit them by hand — a
  hook rejects it.
- **Runbooks** are procedures for recovering or upgrading a subsystem. They are hand-written.
- **Design** documents record how a subsystem was built and why.

## What generates what

The reference pages read `containers_list` in `ansible/inventory/host_vars/`, the per-role
IngressRoute macro calls, the Longhorn backup-tier volume lists, and each role's
`k8s_autodeploy` declaration. A fact none of those sources carries prints its reason instead of
a value.

## Reading a page's freshness

Two separate signals, because they answer different questions:

- A page's `generated_at` frontmatter says when **that page's content** last changed. A page
  that has not changed in months is normal.
- The **site build time** below says when the refresh cron last ran. A site that has not been
  built in weeks means the cron stopped.

<p id="build-stamp" class="build-stamp"></p>

<script>
// build-info.json is written into the built site by scripts/build_docs.py and is never
// committed — keeping it out of the repo is what stops every cron run producing a commit.
// Degrades silently: a missing file leaves the line empty rather than showing an error.
fetch("/build-info.json")
  .then((r) => (r.ok ? r.json() : null))
  .then((d) => {
    if (d && d.built_at) {
      document.getElementById("build-stamp").textContent =
        "Site last built: " + d.built_at;
    }
  })
  .catch(() => {});
</script>
