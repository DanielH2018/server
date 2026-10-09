"""Ansible filter plugin deriving plain Prometheus scrape jobs from `metrics` on containers_list.

An exporter's metrics port is declared once, as a `metrics` item on its `containers_list` entry
(#3743). Two consumers read it:

- `scrape_jobs`: observability's `prometheus.yaml.j2` renders one static job per item, so a
  plain exporter needs no hand-copied `<service>.<namespace>.svc:<port>` target.
- `metrics_port`: the owning role's Service and container read the same port by job name, so
  the scrape target and the listener cannot drift apart.

A `metrics` item is a mapping with these keys:

- `job` (required): the Prometheus `job_name`. Dashboards and monitor-bridge's
  `EXPORTER_DEPENDENT` key on it, so it is spelled out rather than derived from the entry name.
- `port`: the port the Service exposes the metrics on. Defaults to the entry's own `port`, for a
  workload that serves its metrics beside its UI.
- `service`: the Service name in the target. Defaults to the entry's `name`.
- `path`: the `metrics_path`, when it is not `/metrics`.
- `interval`: the job's `scrape_interval`, when it is not the global one.
- `replicas_var`: the variable holding the workload's replica count. The template drops the job
  while that count is 0, because a Service with no endpoint reads `up == 0` and pages the
  scrape-target check.

A job needing auth, params, relabeling or pod discovery stays hand-written in the template. An
unknown key raises rather than being ignored, so a `params:` written here fails the render
instead of rendering a job without it.

No Ansible import, so the render harnesses register the same function the playbook runs.
Ansible wraps a filter's `ValueError` in its own error.
"""

_REQUIRED = ("job",)
_OPTIONAL = ("port", "service", "path", "interval", "replicas_var")


def _items(entry):
    """The entry's `metrics` items, each checked for keys. Raises ValueError on a bad one."""
    items = entry.get("metrics") or []
    if not isinstance(items, list):
        raise ValueError(
            f"containers_list entry {entry.get('name')!r}: `metrics` must be a list of "
            f"mappings, got {type(items).__name__}"
        )
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(
                f"containers_list entry {entry.get('name')!r}: a `metrics` item must be a "
                f"mapping, got {item!r}"
            )
        missing = [k for k in _REQUIRED if k not in item]
        unknown = sorted(set(item) - set(_REQUIRED) - set(_OPTIONAL))
        if missing or unknown:
            raise ValueError(
                f"containers_list entry {entry.get('name')!r}: `metrics` item {item!r} "
                f"lacks {missing} or carries unknown keys {unknown}; known: "
                f"{', '.join(_REQUIRED + _OPTIONAL)}. A job needing anything else stays "
                "hand-written in prometheus.yaml.j2"
            )
    return items


def _port(entry, item):
    port = item.get("port", entry.get("port"))
    if port is None:
        raise ValueError(
            f"containers_list entry {entry.get('name')!r}: `metrics` job {item['job']!r} "
            "names no `port` and the entry has none to default to"
        )
    return int(port)


def scrape_jobs(containers_list, namespace):
    """One plain scrape job per `metrics` item of `containers_list`, in list order.

    Each job is a dict with `job`, `target` (`<service>.<namespace>.svc:<port>`) and, when the
    item sets them, `path`, `interval` and `replicas_var`.

    Raises ValueError on a malformed item, or when two items name the same job.
    """
    jobs, seen = [], {}
    for entry in containers_list or []:
        if not isinstance(entry, dict):
            continue
        for item in _items(entry):
            name = item["job"]
            if name in seen:
                raise ValueError(
                    f"scrape job {name!r} is declared by both {seen[name]!r} and "
                    f"{entry.get('name')!r}"
                )
            seen[name] = entry.get("name")
            service = item.get("service", entry.get("name"))
            job = {
                "job": name,
                "target": f"{service}.{namespace}.svc:{_port(entry, item)}",
            }
            for key in ("path", "interval", "replicas_var"):
                if key in item:
                    job[key] = item[key]
            jobs.append(job)
    return jobs


def metrics_port(containers_list, job):
    """The port of the `metrics` item named `job`, wherever in `containers_list` it sits.

    Looked up by job rather than read off `container_item`, so a template reads the same port
    whichever entry is current when it renders.

    Raises ValueError when no entry declares `job`.
    """
    for entry in containers_list or []:
        if not isinstance(entry, dict):
            continue
        for item in _items(entry):
            if item["job"] == job:
                return _port(entry, item)
    raise ValueError(f"no containers_list entry declares a `metrics` job {job!r}")


class FilterModule:
    def filters(self):
        return {"scrape_jobs": scrape_jobs, "metrics_port": metrics_port}
