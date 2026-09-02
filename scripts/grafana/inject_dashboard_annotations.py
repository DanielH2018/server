#!/usr/bin/env python3
"""Add the deploy-annotation query to every provisioned Grafana dashboard, from one place.

Grafana has no global annotation setting: an annotation query lives in each dashboard's own
JSON, and all sixteen boards here ship `"annotations": {"list": []}`. Editing sixteen files by
hand — and every future board too — is the kind of fan-out that gets half-done, so the query is
injected at deploy time instead.

WHY A SEPARATE OUTPUT DIRECTORY, not an in-place edit of the staged copy. `dashboards.yml`
stages `files/dashboards/` onto the node with `ansible.builtin.copy`, which compares checksums.
Rewriting those files in place would make that copy task see a mismatch on the NEXT deploy,
re-copy the pristine JSON, and the injector would re-inject — reporting `changed` forever and
rolling Grafana on every single deploy. Reading from the staged tree and writing to a derived
one keeps both halves idempotent: same input, same output, no rollout.

The source JSON under `files/dashboards/` is never touched, which matters because
`scripts/grafana/export_grafana_dashboards.py` round-trips boards from the Grafana UI back into it. An
injected annotation must not become something a UI export commits back as source — so the
injection is idempotent by name as well, and a board that already declares an annotation with
this name is left exactly as it is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The name is the idempotency key. Renaming it re-injects alongside the old one rather than
# replacing it, so treat this as a fixed identifier rather than a label to tune.
ANNOTATION_NAME = "Deploys"


def build_annotation(uid: str, expr: str) -> dict:
    """The annotation query, as Grafana's dashboard JSON expects it.

    `logfmt` is what makes the text useful: without it the annotation shows the whole raw
    syslog line, and with it `textFormat` can pull out just the service list.
    """
    return {
        "name": ANNOTATION_NAME,
        "datasource": {"type": "loki", "uid": uid},
        "enable": True,
        "hide": False,
        "iconColor": "purple",
        "target": {"expr": expr, "refId": "DeployAnno", "queryType": "range"},
        "textFormat": "{{services}}",
        "titleFormat": "deploy",
    }


def inject(doc: dict, annotation: dict) -> bool:
    """Add `annotation` to a dashboard doc. True if the doc changed.

    Grafana tolerates a missing `annotations` key, but the ConfigMap is easier to diff when
    every board has the same shape, so the container is created when absent.
    """
    annotations = doc.setdefault("annotations", {})
    entries = annotations.setdefault("list", [])

    if any(entry.get("name") == ANNOTATION_NAME for entry in entries):
        return False

    entries.append(annotation)
    return True


def process_tree(src: Path, dest: Path, annotation: dict) -> tuple[int, int]:
    """Copy every dashboard from `src` to `dest`, injecting as it goes.

    Returns (written, injected). `written` counts every board that reached `dest`, so a board
    that already had the annotation is still copied — `dest` must be a COMPLETE tree, since the
    ConfigMap is built from it and a board missing here is a board missing from Grafana.
    """
    written = injected = 0

    for source_file in sorted(src.rglob("*.json")):
        doc = json.loads(source_file.read_text())
        if inject(doc, annotation):
            injected += 1

        target = dest / source_file.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys so the output is a pure function of the input: an unstable key order would
        # change the ConfigMap on every run and roll Grafana for no reason.
        rendered = json.dumps(doc, indent=2, sort_keys=True) + "\n"

        # Write only on a real difference, so file mtimes (and Ansible's view of the tree) stay
        # stable across no-op runs.
        if not target.exists() or target.read_text() != rendered:
            target.write_text(rendered)
        written += 1

    return written, injected


def main() -> int:
    """Copy every dashboard from `--src` to `--dest`, injecting the deploy annotation.

    Exits 1 if `--src` isn't a directory or if the copy produced no dashboards at all
    (an empty `--dest` would otherwise remove every board from Grafana).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument(
        "--uid",
        required=True,
        help="the loki-homelab datasource uid Grafana provisions",
    )
    parser.add_argument(
        "--expr",
        required=True,
        help="the LogQL query selecting deploy events",
    )
    args = parser.parse_args()

    if not args.src.is_dir():
        print(f"source tree not found: {args.src}", file=sys.stderr)
        return 1

    written, injected = process_tree(
        args.src, args.dest, build_annotation(args.uid, args.expr)
    )

    if not written:
        # An empty tree means the staging step silently produced nothing, and the ConfigMap
        # built from `dest` would then REMOVE every dashboard from Grafana. Fail rather than
        # let a deploy quietly empty the boards.
        print(f"no dashboards found under {args.src}", file=sys.stderr)
        return 1

    print(f"{written} dashboard(s) written, {injected} annotated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
