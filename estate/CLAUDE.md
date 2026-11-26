# `estate/` — facts about the whole estate

ADR-0042 (`docs/adr/0042-each-variable-lives-with-what-it-describes.md`) puts each variable
beside the thing it describes. This directory holds the facts that are true of the estate as a
whole, whichever plane reads them: the domain, the time zone, the LAN subnets, the interpreter
pin and the CI contexts. They live in `estate/vars.yml`.

## Which file a variable goes in

| The variable describes | File |
|---|---|
| the estate: a name, a subnet, a schedule every plane shares | `estate/vars.yml` |
| the cluster: a CIDR, a VIP, a namespace, a k8s image pin | `k8s/vars.yml` |
| every host: a package list, a `has_*` default | `hosts/vars.yml` |
| one host | `hosts/<host>/vars.yml` |
| one component or role, and nothing else reads it | that component's or role's `defaults/main.yml` |

The renders layer them in this order, later files winning:

```text
host render:     component defaults < estate < k8s < hosts < hosts/<host>
package render:  component defaults < estate < k8s < hosts
k8s render:      role defaults      < estate < k8s
```

A k8s role reads a host's value through `hosts[<host>].<key>`, never as a bare name.

## Rules a reader of this directory meets

- **Python reads these files through `lib.estate`**, never by path. The
  `inventory-files-are-read-through-lib-estate` row of
  `scripts/tests/test_census_rows_test_modules.py` refuses a raw read.
- **`host_python_version` stays one line of the form `host_python_version: "3.x.y"`.**
  `hosts/bootstrap-host.sh` and `hosts/lib/builder_python.py` read it with a line match,
  because they run before PyYAML is installed.
- **A Renovate PR that changes a file here is refused** by
  `hosts/components/renovate-agent/files/land_renovate_pr.py`. A pin here reaches every role
  that reads it, denylisted ones included, so an interactive session lands it.
- **No secret goes here.** Secrets live in `secrets/secrets.yml`.
