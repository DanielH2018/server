# Homelab docs

Reference and runbooks for this homelab: `daniel-box` (k3s server), `daniel-server` (k3s
agent), and `daniel-pi` (the one remaining Docker host). `daniel-stage` is a fourth host in
the inventory and is not one of these — it is the staging cluster, and the
[Hosts reference](reference/hosts.md) lists every host the inventory declares.

## Where to start

- **Reference** pages are generated from the Ansible tree. They carry the timestamp and commit
  their content was last built from, and a cron regenerates them. Do not edit them by hand — a
  hook rejects it.
- **Decisions** are the architecture-decision records: what was chosen, why, and what it cost.
  A decision still in force lives there; the state it produced lives in the reference pages.
  Its *Background* pages are the long-form working-out behind three of those records.
- **Operations** are the hand-written procedures, ordered day-to-day first and recovery last:
  deploying, rotating a secret, then upgrading or restoring a subsystem after something broke.
- **Artifacts** is not a page. It is a link off this site to the artifact browser.

## What generates what

The reference pages read `containers_list` in `ansible/inventory/host_vars/`, the per-role
IngressRoute macro calls, the Longhorn backup-tier volume lists, and each role's
`k8s_autodeploy` declaration. A fact none of those sources carries prints its reason instead of
a value.

The scripts page reads each script's own module docstring, parsed rather than imported. So the
way to change what it says about a script is to change that script's docstring.

## Reading a page's freshness

Two separate signals, because they answer different questions:

- A page's `generated_at` frontmatter says when **that page's content** last changed. A page
  that has not changed in months is normal.
- The **site build time** below says when the refresh cron last ran. A site that has not been
  built in weeks means the cron stopped.

A hand-written page has no `generated_at`, so it carries a footer instead: the date of its
last commit, and which of the repo files it names changed after that. A moved source does
not make the page wrong, it makes it the page to reread next. The
[freshness table](reference/freshness.md) ranks every hand-written page that way.

<p id="build-stamp" class="build-stamp"></p>

<script>
// build-info.json is written into the built site by scripts/docs/build_docs.py and is never
// committed — keeping it out of the repo is what stops every cron run producing a commit.
// Degrades silently: a missing file leaves the line empty rather than showing an error.
fetch("/build-info.json")
  .then((r) => (r.ok ? r.json() : null))
  .then((d) => {
    if (d && d.built_at) {
      // The generators status matters as much as the timestamp. Two of docs-refresh.sh's
      // paths (dirty tree, open PR) rebuild the site without regenerating the pages, and a
      // bare timestamp reads identically to a full run — so a stuck-open PR served pages
      // that got staler every twelve hours under a stamp that said "just built".
      var note = "Site last built: " + d.built_at;
      if (d.generators && d.generators !== "ok") {
        note += " — pages NOT regenerated this run (" + d.generators + ")";
      }
      document.getElementById("build-stamp").textContent = note;
    }
  })
  .catch(() => {});
</script>
