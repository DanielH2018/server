#!/usr/bin/env python3
"""Export the *customized* Grafana dashboards from the live DB into code.

This is the DB->code mirror of ``fetch_grafana_dashboards.py`` (which is grafana.com->code):

  * ``fetch_grafana_dashboards.py``  — boards whose upstream is grafana.com
    (Node Exporter Full, cAdvisor, Traefik 17346). Re-derived from the community export.
  * ``export_grafana_dashboards.py`` (this file) — boards whose upstream is the *live
    Grafana DB* because their customizations only exist there: the CrowdSec set, the Loki
    log views (logfmt/datasource fixes), "Docker and system monitoring", etc.

It dumps every ``dash-db`` dashboard except the community-fetched ones (``SKIP_UIDS``),
preserving the live folder structure as subdirectories (the provider has
``foldersFromFilesStructure: true``), so a fresh Grafana rebuild reproduces the same boards
in the same folders. UI edits are captured back into version control by re-running this.

    python3 scripts/export_grafana_dashboards.py

Requires a running ``grafana`` container; all API calls go through ``docker exec grafana
wget`` against ``localhost:3000`` with the admin password read from its mounted file
(``GF_SECURITY_ADMIN_PASSWORD__FILE``; the bare env var is empty since the password was
file-mounted) so special characters in the password never have to survive URL/shell
quoting. Idempotent; overwrites the JSON in the role.
"""

import base64
import json
import os
import re
import subprocess

OUTDIR = "ansible/roles/containers/grafana/files/dashboards"

# Canonical datasource uids we provision (see provisioning/datasources.yml.j2).
PROM_UID, LOKI_UID = "EGdsQqhVk", "bf4q19tuivta8e"

# Dashboards managed by fetch_grafana_dashboards.py (upstream = grafana.com). Skip them
# here so the two scripts own disjoint files and never clobber each other.
SKIP_UIDS = {
    "rYdddlPWk",  # Node Exporter Full  -> node-exporter-full.json
    "pMEd7m0Mz",  # Cadvisor exporter   -> cadvisor.json
    "n5bu_kv45",  # Traefik 17346 — RETIRED (merged into traefik-custom); skip so a
    # pre-deploy export can't resurrect the deleted board as a new custom file.
}

# Stale/foreign datasource uids found in hand-imported boards, remapped onto the canonical
# Prometheus datasource so every panel resolves. (CrowdSec "Details per Machine" shipped a
# panel pointing at a datasource uid that no longer exists -> "datasource not found".)
DS_UID_REMAP = {"IH0jqv6nz": PROM_UID}

# Title slug -> filename would collide with a community file; force a distinct name.
FILENAME_OVERRIDE = {"ddmlqvk12uozka": "traefik-custom"}


def gapi(path):
    """GET a Grafana API path via the container, authenticated as admin."""
    pw = subprocess.run(
        # The admin password is file-mounted (GF_SECURITY_ADMIN_PASSWORD__FILE) so it stays
        # out of the container env — the bare GF_SECURITY_ADMIN_PASSWORD var is empty. Read
        # the file it points at.
        [
            "docker",
            "exec",
            "grafana",
            "sh",
            "-c",
            'cat "$GF_SECURITY_ADMIN_PASSWORD__FILE"',
        ],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    auth = base64.b64encode(("admin:%s" % pw).encode()).decode()
    out = subprocess.run(
        [
            "docker",
            "exec",
            "grafana",
            "wget",
            "-qO-",
            "--header=Authorization: Basic " + auth,
            "http://localhost:3000" + path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return json.loads(out)


def slug(title):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")


def normalize(obj):
    """Recursively clean a dashboard model in place:

    * rewrite stale datasource uids onto the canonical ones, and
    * drop the ephemeral ``key`` on query targets — it's a Grafana query-editor React
      key (a random UUID Grafana regenerates), not config. Keeping it adds churn and
      trips secret scanners (high-entropy string assigned to ``key``).
    """
    if isinstance(obj, dict):
        ds = obj.get("datasource")
        if isinstance(ds, dict) and ds.get("uid") in DS_UID_REMAP:
            ds["uid"] = DS_UID_REMAP[ds["uid"]]
        elif isinstance(ds, str) and ds in DS_UID_REMAP:
            obj["datasource"] = DS_UID_REMAP[ds]
        if "refId" in obj:  # this dict is a query target
            obj.pop("key", None)
        for v in obj.values():
            normalize(v)
    elif isinstance(obj, list):
        for v in obj:
            normalize(v)


def main():
    index = gapi("/api/search?type=dash-db")
    untracked = []
    for entry in sorted(index, key=lambda e: e["title"]):
        uid, title = entry["uid"], entry["title"]
        if uid in SKIP_UIDS:
            continue
        folder = entry.get("folderTitle") or "General"
        d = gapi("/api/dashboards/uid/%s" % uid)["dashboard"]
        normalize(d)
        d["id"] = None  # let Grafana assign a local id; the stable `uid` is the key
        # DB save-counter, not config — provisioning reloads bump it, so leaving it in
        # makes every later drift-check dirty even when no panel changed.
        d["version"] = 1

        name = FILENAME_OVERRIDE.get(uid, slug(title))
        subdir = "" if folder == "General" else folder
        dest_dir = os.path.join(OUTDIR, subdir)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name + ".json")
        with open(dest, "w") as fh:
            fh.write(json.dumps(d, indent=2) + "\n")
        rel = os.path.relpath(dest, OUTDIR)
        print("%-38s %-30s -> %s" % (uid, title, rel))
        untracked.append(uid)

    # Surface anything live that this run captured, plus a sanity count.
    print(
        "\nExported %d dashboard(s); skipped %d community board(s)."
        % (len(untracked), len(SKIP_UIDS))
    )


if __name__ == "__main__":
    main()
