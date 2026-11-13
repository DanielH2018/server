#!/usr/bin/env python3
"""Scaffold a k3s service role from its name, image and port, instead of copying a sibling.

Usage::

    uv run python scripts/dev/new_k8s_service.py miniflux \\
        --image ghcr.io/miniflux/miniflux:2.2.16 --port 8080 --authelia one_factor

The role is a `containers_list` entry plus a Deployment template (#4298). The entry is
appended to `cluster/services.yml`, and `templates/deployment.yaml.j2`
is the one manifest the role ships. The role has no `tasks/`: the renderer derives the file
lists from `templates/` and the entry (#3662), and the workload to restart from the role's name.
Two small files sit beside the template:

* `defaults/main.yml` holds what has no other home: the image pin Renovate's k8s-defaults
  manager bumps, the `k8s_autodeploy` stance `lib.k8s_roles.k8s_autodeploy_denylist` raises
  without, the resources, and the pinned uid.
* `CLAUDE.md` carries the `## At a glance` markers. `scripts/docs/gen_role_glance.py`, which
  this script runs last, writes the tag, image, route, claims and stance between them, so the
  rest of the doc holds only what the generator cannot derive.

WHY A GENERATOR AND NOT A SIBLING (#2855). Step 1 of the `new-k8s-service` skill used to say
"copy a close sibling". A sibling's files carry its narration, and that narration is dated: the
littlelink role's Deployment cited a Compose template that had not existed since 2026-08-14.
A role carrying someone else's history is a worse starting point than one carrying none.

WHAT IT DOES NOT WRITE, deliberately:

* No `service.yaml.j2`. The renderer renders `ansible/templates/service-default.yaml.j2`
  for a role that ships none (#2872), reading the name and port off the entry this script
  writes. A Service that needs more than that is a decision, so it stays a hand edit.
* No PVC, no Secret, no NetworkPolicy, no probes beyond the readiness/liveness pair on the
  service's own port. Each is a decision about the service, and a generated placeholder for a
  decision is the narration problem again in a new form.
* No deploy. Flux applies every k8s entry once the PR merges and `promote` publishes the next
  bundle, and `land.sh` follows the promotion.

The output is meant to pass `prek run --all-files` and the renderer's `cluster` render of the
new tag with no hand edits; `scripts/dev/tests/test_new_k8s_service.py` renders it through
the repo's own manifest validator to hold that.
"""

import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser

from lib.repo_paths import K8S_ROLES, K8S_SERVICES, REPO


from renderer.template_filters.toposort import K8S_APPLIES_LAST

AUTH_TIERS = ("one_factor", "two_factor")
STRATEGIES = ("RollingUpdate", "Recreate")

# The tiers `ansible/roles/k8s/cluster-base/templates/priorityclass.yaml.j2` declares. Named here so a
# typo fails at the command line rather than at admission, hours later.
PRIORITY_CLASSES = (
    "homelab-critical",
    "homelab-standard",
    "homelab-best-effort",
)


def var_prefix(name: str) -> str:
    """The role's variable prefix: `bento-pdf` owns `bento_pdf_k8s_*`."""
    return name.replace("-", "_")


def defaults_main(
    name: str, image: str, autodeploy: bool, reason: str, uid: int
) -> str:
    prefix = var_prefix(name)
    stance = "true" if autodeploy else "false"
    return f"""---
# Renovate's k8s-defaults manager tracks this pin. Keep the tag alongside a digest
# (`repo:tag@sha256:...`) if you pin by digest — a bare `repo@sha256:...` freezes the image
# with no update signal at all.
{prefix}_k8s_image: {image}

# Renovate stance: whether an image bump here may automerge. `promote` deploys what merges.
# `lib.k8s_roles.k8s_autodeploy_denylist` raises on a role that declares neither. The reason is what makes the stance reviewable, so replace the text below
# with the property that actually decides it.
k8s_autodeploy: {stance}
k8s_autodeploy_reason: "{reason}"

{prefix}_k8s_cpu_limit: "500m"
{prefix}_k8s_mem_limit: 256Mi
{prefix}_k8s_cpu_request: "50m"
{prefix}_k8s_mem_request: 64Mi

# The uid the container runs as, pinned rather than left to the image's named user.
# `ansible/tests/k8s/test_container_security_context.py::test_every_asserting_container_pins_a_uid`
# requires it: a pod asserting runAsNonRoot with no runAsUser still passes admission after a
# tag bump moves that named user to a different uid. Read the image's own uid and set it here.
{prefix}_k8s_uid: {uid}
"""


def deployment_template(name: str, strategy: str, priority_class: str) -> str:
    prefix = var_prefix(name)
    return f"""{{% from 'container-resources.yml.j2' import container_resources with context %}}
{{% from 'security-context.yml.j2' import hardened_security_context with context %}}
{{% from 'workload-shell.yml.j2' import spec_shell, pod_shell with context %}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {{{{ k8s_namespace }}}}
spec:
{{{{ spec_shell('{strategy}') }}}}
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
        netpol-baseline: enforced
    spec:
{{{{ pod_shell('{priority_class}', run_as_user={prefix}_k8s_uid, run_as_group={prefix}_k8s_uid, fs_group={prefix}_k8s_uid) }}}}
      containers:
        - name: {name}
          image: {{{{ {prefix}_k8s_image }}}}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: {{{{ container_item.port }}}}
{{{{ hardened_security_context(read_only=true, non_root=true) }}}}
          readinessProbe:
            httpGet:
              path: /
              port: {{{{ container_item.port }}}}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /
              port: {{{{ container_item.port }}}}
            initialDelaySeconds: 15
            periodSeconds: 30
{{{{ container_resources(cpu_limit={prefix}_k8s_cpu_limit, mem_limit={prefix}_k8s_mem_limit, cpu_request={prefix}_k8s_cpu_request, mem_request={prefix}_k8s_mem_request) }}}}
"""


def role_doc(name: str, route: bool) -> str:
    """A CLAUDE.md whose `## At a glance` block the generator fills in immediately after.

    The markers have to be present and empty: `gen_role_glance.py` writes between them and
    leaves everything else alone, so a doc without them is never filled. The block states the
    deploy tag, image, route and claims, so the prose below it states
    none of them. It keeps the one fact the block omits: the Service and the route render
    from shared templates rather than from this role.
    """
    shared = (
        "The Service and the IngressRoute render from "
        "`ansible/templates/service-default.yaml.j2` and "
        "`ansible/templates/ingressroute-default.yaml.j2`, off the containers_list entry."
        if route
        else "The Service renders from `ansible/templates/service-default.yaml.j2`, off the "
        "containers_list entry. No IngressRoute: callers reach it in-cluster by Service name."
    )
    return f"""# {name} — TODO: one line on what this service is for

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
<!-- /generated_from -->

## Notable
- TODO: what a reader would get wrong about this role. Delete this heading if nothing does.

## Editing
- Manifests: `templates/`. {shared}
- Image pin, Renovate stance, resources and uid: `defaults/main.yml`.
"""


def entry_lines(name, port, hostname, auth_tier, route) -> str:
    """The service entry, as the block this script appends to cluster/services.yml.

    Position in the list does not set the deploy order: `scripts/renderer/template_filters/toposort.py`
    derives the ordering edges from the templates and the entry, and gives each
    `K8S_APPLIES_LAST` role (netpol-baseline) an edge onto every other entry. An edge no
    template carries goes in `depends_on:` here, never in the position.

    Every entry is Flux's; it carries no `owner` key (H1.9).
    """
    lines = [f"  - name: {name}"]
    if route:
        lines.append(f'    hostname: "{hostname}"')
    lines.append(f"    port: {port}")
    if auth_tier:
        lines.append("    use_authelia: true")
        lines.append(f"    auth_tier: {auth_tier}")
    elif route:
        lines.append("    use_authelia: false")
    return "\n".join(lines) + "\n"


def append_entry(services: Path, entry: str) -> None:
    """Append the entry to `containers_list`, ahead of the first `K8S_APPLIES_LAST` role.

    After the LIST's last item, not the file's last line: a top-level key after
    `containers_list` would otherwise hold an entry written at the end of the file, where it
    parses as a malformed top-level mapping. Appended within the
    list rather than inserted alphabetically because the list's order does not set the deploy
    order — `scripts/renderer/template_filters/toposort.py` derives that from the entries and templates.

    A `K8S_APPLIES_LAST` entry (netpol-baseline) and the comment block above it stay after the
    new entry. The deploy applies it last wherever it sits, but
    `test_derived_order_matches_todays_hand_ordered_list` holds the list to the sorted order,
    so an entry written after it would turn that test red.

    Raises:
        ValueError: if the file declares no `containers_list`, which means the anchor this
            walk needs is gone rather than that the entry belongs at the end.
    """
    lines = services.read_text().splitlines(keepends=True)
    try:
        start = next(
            i for i, line in enumerate(lines) if line.rstrip() == "containers_list:"
        )
    except StopIteration:
        raise ValueError(f"{services} declares no containers_list") from None

    # The list ends at the first non-blank line indented no deeper than the key itself: the
    # next top-level key, or the comment block above it.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t")):
            end = i
            break
    # Blank lines before that key belong to the separation, not to the list.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    # Ahead of the first applies-last item, and of the comment lines directly above it.
    applies_last = {f"  - name: {name}" for name in K8S_APPLIES_LAST}
    first_last = next(
        (i for i in range(start + 1, end) if lines[i].rstrip() in applies_last), None
    )
    if first_last is not None:
        end = first_last
        while end > start + 1 and lines[end - 1].startswith("  #"):
            end -= 1

    services.write_text("".join(lines[:end]) + entry + "".join(lines[end:]))


def write_role(args, roles_dir: Path | None = None) -> list[Path]:
    """Write every file the role needs. Returns the paths written, in write order.

    Args:
        args: the parsed command line.
        roles_dir: where `<name>/` is created. Defaults to `ansible/roles/k8s`; a test passes
            a temporary directory rather than patching the module-level constant.
    """
    role = (roles_dir or K8S_ROLES) / args.name
    files = {
        role / "defaults" / "main.yml": defaults_main(
            args.name, args.image, args.autodeploy, args.autodeploy_reason, args.uid
        ),
        role / "templates" / "deployment.yaml.j2": deployment_template(
            args.name, args.strategy, args.priority_class
        ),
        role / "CLAUDE.md": role_doc(args.name, args.route),
    }
    written = []
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)
    return written


def parse_args(argv=None):
    parser = ArgumentParser(
        description="Scaffold a k3s service role and its containers_list entry.",
        epilog="Review the TODOs in the generated CLAUDE.md, then commit and land the PR with "
        "land.sh. Flux applies the tag once `promote` publishes the merge.",
    )
    parser.add_argument("name", help="service and role name, e.g. miniflux")
    parser.add_argument("--image", required=True, help="image reference for the pin")
    parser.add_argument(
        "--port", required=True, type=int, help="the port the container listens on"
    )
    parser.add_argument(
        "--hostname",
        help="route hostname; defaults to the service name. Ignored with --no-route.",
    )
    parser.add_argument(
        "--authelia",
        choices=AUTH_TIERS,
        help="put the route behind Authelia at this tier. Pick two_factor for anything a "
        "stolen password alone can act on.",
    )
    parser.add_argument(
        "--no-route",
        dest="route",
        action="store_false",
        help="in-cluster only: write no IngressRoute and no hostname",
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default="RollingUpdate",
        help="Deployment strategy. Recreate means a downtime gap on every deploy and has to "
        "be allowlisted with a reason in ansible/tests/k8s/test_deploy_strategy.py.",
    )
    parser.add_argument(
        "--priority-class", choices=PRIORITY_CLASSES, default="homelab-best-effort"
    )
    parser.add_argument(
        "--uid",
        type=int,
        default=1000,
        help="the uid the container runs as, pinned in runAsUser/runAsGroup/fsGroup. Read it "
        "off the image rather than taking the default.",
    )
    parser.add_argument(
        "--no-autodeploy",
        dest="autodeploy",
        action="store_false",
        help="declare k8s_autodeploy: false (see --autodeploy-reason)",
    )
    parser.add_argument(
        "--autodeploy-reason",
        default="TODO: the property that decides this stance",
        help="the reason written beside k8s_autodeploy",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing role directory"
    )
    parser.add_argument(
        "--skip-glance",
        action="store_true",
        help="do not run scripts/docs/gen_role_glance.py afterwards",
    )
    args = parser.parse_args(argv)
    args.hostname = args.hostname or args.name
    return args


def next_steps(args, role: Path) -> str:
    """The follow-up instructions `main` prints once the role and entry are written."""
    route_census = (
        ", and ROLES_WITH_A_DEFAULT_INGRESSROUTE beside it for its route"
        if args.route
        else ""
    )
    # The route's blackbox target renders in observability, and until Flux applies that render
    # `probe.py health` skips the probe half (#5346).
    probe_step = (
        f"Once Flux has applied `observability`, which loads {args.name}'s ingress probe, "
        f"gate it with `probe.py health {args.name}`.\n"
        if args.route
        else ""
    )
    return (
        f"\nNext: fill the TODOs in {(role / 'CLAUDE.md').relative_to(REPO)}, then\n"
        f"  prek run --all-files\n"
        f"  uv run python scripts/renderer/homelab_render.py cluster --out out --role {args.name}\n"
        f"Flux applies {args.name} once the PR merges and `promote` "
        f"publishes it: see the new-k8s-service skill.\n"
        f"{probe_step}"
        f"Secrets and NetworkPolicies are not scaffolded. Add each as a template under "
        f"{(role / 'templates').relative_to(REPO)}; the renderer renders every top-level "
        f"one, and a name containing `secret` renders 0600 under no_log. A PVC is a "
        f"`k8s_claims` entry in {(role / 'defaults' / 'main.yml').relative_to(REPO)}, not "
        f"a template.\n"
        f"\nTwo censuses a new role always has to join, which `prek` does not run and CI does:\n"
        f"  BORN_FENCED_ROLES in ansible/tests/k8s/test_netpol_baseline_labels.py — add "
        f"{args.name} with the sentence saying why Traefik is its only caller, or drop the "
        f"`netpol-baseline: enforced` pod label and write the role its own NetworkPolicy.\n"
        f"  ROLES_WITH_A_DEFAULT_SERVICE in "
        f"ansible/tests/k8s/test_shared_manifest_defaults.py — add {args.name}, since it "
        f"takes the shared default Service{route_census}.\n"
        f"The regen-doc-fragments prek hook rewrites the Renovate stance fragment on "
        f"your commit and fails once: stage what it wrote and commit again."
    )


def main(argv=None) -> int:
    args = parse_args(argv)

    role = K8S_ROLES / args.name
    if role.exists() and not args.force:
        print(
            f"error: {role.relative_to(REPO)} already exists; pass --force to overwrite",
            file=sys.stderr,
        )
        return 1
    if args.authelia and not args.route:
        print(
            "error: --authelia attaches the middleware to a route, and --no-route writes "
            "none. Drop one of the two.",
            file=sys.stderr,
        )
        return 1

    for path in write_role(args):
        print(f"wrote {path.relative_to(REPO)}")
    append_entry(
        K8S_SERVICES,
        entry_lines(args.name, args.port, args.hostname, args.authelia, args.route),
    )
    print(f"appended the containers_list entry to {K8S_SERVICES.relative_to(REPO)}")

    if not args.skip_glance:
        subprocess.run(
            [sys.executable, str(REPO / "scripts" / "docs" / "gen_role_glance.py")],
            check=True,
        )

    print(next_steps(args, role))
    return 0


if __name__ == "__main__":
    sys.exit(main())
