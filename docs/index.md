# Homelab docs

Reference and runbooks for this homelab: `daniel-box` (k3s server), `daniel-server` (k3s
agent), and `daniel-pi` (the one remaining Docker host). The
[Hosts reference](reference/hosts.md) lists every host the inventory declares.

## Where to start

- **Reference** pages are generated from this repository by the scripts under `scripts/docs/`. They carry the timestamp and commit
  their content was last built from, and a cron regenerates them. Do not edit them by hand — a
  hook rejects it.
- **Decisions** are the architecture-decision records: what was chosen, why, and what it cost.
  A decision still in force lives there; the state it produced lives in the reference pages.
  Its *Background* pages are the long-form working-out behind three of those records.
- **Operations** are the hand-written procedures and records. Its first page,
  [How the lab is built](what-the-migration-built.md), describes the three planes that each
  have one write path from git, and links the page that documents each. The groups after it
  are ordered day-to-day first and recovery last: deploying, CI, monitoring, access, Claude
  tooling and each role's record, then upgrading or restoring a subsystem after something
  broke.
  [Break glass](break-glass.md) is the last one — the single entry point for rebuilding or
  recovering the lab when the author is unavailable or daniel-box is gone.
- **The archive** is not in the navigation. It is the record of work that shipped, spikes
  that concluded and workloads that retired, and none of it is instructions. The
  [archive index](archive/README.md) lists each archived page and names the page that
  documents its result.
- **Artifacts** is not a page. It is a link off this site to the artifact browser.

## Commands in runbooks

The operator runs as `ubuntu`, so a command in a runbook runs from that shell as written.
A command that needs root, such as every `k3s` subcommand, starts with `sudo`. A step that
holds a root shell opens its code block with `sudo -i` and shows the commands after it without
`sudo`. the `runbook-k3s-commands-carry-sudo` row of `tests/repo/test_census_rows_text.py` fails on a fenced `k3s` command that has
neither. Plain `kubectl` needs no `sudo`, because the `ubuntu` kubeconfig reads the cluster.

## What generates what

The reference pages read `containers_list` in `k8s/services.yml`, the Pi's `compose_services`,
the per-role IngressRoute macro calls, the Longhorn backup-tier volume lists, and each role's
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
      // The generators status matters as much as the timestamp. docs-refresh.sh's open-PR
      // path rebuilds the site without regenerating the pages, and a
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
