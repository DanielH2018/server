"""A fixture `hosts/` tree around the real pi-compose component, and the census over its project.

`ComposeTree` copies the real component's template and tmpfiles file into a scratch tree,
adds the `compose:` and `outputs:` keys that H5.2b gives the real `component.yml`, and writes
fixture service trees beside them. `project_of` reads the rendered package back as the Compose
project it describes, and the `*_problems` functions are the censuses H5.2a1 ships. They run
over fixture projects now. Each move PR (H5.6 to H5.9) adds its service to `MOVED_MEMBERS` in
the census test once the live component holds it.
"""

import json
import re
import shutil
from pathlib import PurePosixPath

import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

REAL = REPO / "hosts" / "components" / "pi-compose"
SCRIPT = REPO / "hosts" / "lib" / "homelab-secrets"
COMPOSE = "/usr/lib/homelab/compose"
NAMES = ("domain", "monitor_discord_webhook_url", "alloy_pi_http_password")
DIGEST = "@sha256:" + "ab" * 32
DATA = "/home/ubuntu/server/containers"
UNIT = (
    "[Unit]\nDescription=fixture\n[Service]\nType=oneshot\n"
    "Environment=HOMELAB_COMPOSE_INPUTS_SHA256={{ compose_inputs_sha256 }}\n"
    "ExecStart=/bin/true\n"
)

AUTOHEAL = f"""services:
  autoheal:
    container_name: autoheal
    image: willfarrell/autoheal:latest{DIGEST}
    environment:
      - WEBHOOK_URL={{{{ monitor_discord_webhook_url }}}}
      - DOCKER_SOCK=tcp://docker-proxy-lifecycle:2375
    depends_on:
      - docker-proxy-lifecycle
"""
PROXY = f"""services:
  docker-proxy:
    container_name: docker-proxy
    image: tecnativa/docker-socket-proxy:latest{DIGEST}
  docker-proxy-lifecycle:
    container_name: docker-proxy-lifecycle
    image: tecnativa/docker-socket-proxy:latest{DIGEST}
    volumes:
      - type: bind
        source: /var/run/docker.sock
        target: /var/run/docker.sock
        bind: {{create_host_path: false}}
"""
ALLOY = f"""services:
  alloy:
    container_name: alloy
    image: grafana/alloy:v1.19.2{DIGEST}
    environment:
      - HOMELAB_DOMAIN={{{{ domain }}}}
    labels:
      homelab.config-sha256: "{{{{ alloy_config_sha256 }}}}"
    depends_on:
      - docker-proxy
    volumes:
      - type: bind
        source: {COMPOSE}/alloy/config.alloy
        target: /etc/alloy/config.alloy
        read_only: true
        bind: {{create_host_path: false}}
      - type: bind
        source: ./data
        target: /data
        bind: {{create_host_path: false}}
      - type: bind
        source: /var/log/pi-health
        target: /var/log/pi-health
        read_only: true
        bind: {{create_host_path: false}}
"""
CONFIG_ALLOY = '// {{ container_item.name }} ships logs\nlogging { level = "info" }\n'
TMPFILES_LINES = "\nd /home/ubuntu/server/containers/alloy/data 0755 ubuntu ubuntu -\n"

SERVICES = {
    "docker-proxy": {"compose.yaml.j2": PROXY},
    "autoheal": {"compose.yaml.j2": AUTOHEAL},
    "alloy": {"compose.yaml.j2": ALLOY, "config.alloy.j2": CONFIG_ALLOY},
}


class ComposeTree:
    """A fixture `hosts/` tree whose daniel-pi package lists `secrets-unit` and `pi-compose`."""

    def __init__(
        self, tmp_path, services=None, keys=("domain", "monitor_discord_webhook_url")
    ):
        self.root = tmp_path / "hosts"
        self.secrets = tmp_path / "secrets.yml"
        self.sops_dir = tmp_path / "host-sops"
        self.component = self.root / "components" / "pi-compose"
        self.secrets.write_text(yaml.safe_dump({name: "ENC[x]" for name in NAMES}))
        self.sops_dir.mkdir()
        (self.sops_dir / "daniel-pi.sops.yaml").write_text("sops: {}\ndomain: ENC[x]\n")
        (self.root / "lib").mkdir(parents=True)
        shutil.copy(SCRIPT, self.root / "lib" / "homelab-secrets")
        unit = self.root / "components" / "secrets-unit"
        unit.mkdir(parents=True)
        (unit / "component.yml").write_text(
            yaml.safe_dump(
                {"files": [{"lib": "homelab-secrets", "dst": "/usr/lib/homelab/s"}]}
            )
        )
        shutil.copytree(REAL, self.component)
        (self.component / "units").mkdir()
        (self.component / "units" / "homelab-compose.service.j2").write_text(UNIT)
        self.services = dict(SERVICES if services is None else services)
        self.keys = list(keys)
        (self.root / "daniel-pi").mkdir()
        (self.root / "daniel-pi" / "package.yml").write_text(
            yaml.safe_dump({"components": ["secrets-unit", "pi-compose"]})
        )
        self.write()

    def write(self, **overrides):
        """Write the services and the component spec; `overrides` replace spec keys."""
        shutil.rmtree(self.component / "services", ignore_errors=True)
        for name, files in self.services.items():
            for relative, text in files.items():
                path = self.component / "services" / name / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        spec = yaml.safe_load((REAL / "component.yml").read_text())
        spec["defaults"] = {"compose_services": [{"name": n} for n in self.services]}
        spec["compose"] = "services"
        spec["outputs"] = [self.output()]
        spec["files"].append(
            {
                "src": "units/homelab-compose.service.j2",
                "dst": "/usr/lib/systemd/system/homelab-compose.service",
                "inputs_sha256": True,
            }
        )
        spec.update(overrides)
        (self.component / "component.yml").write_text(yaml.safe_dump(spec))
        return self

    def output(self, **fields):
        return {
            "src": "compose.env.j2",
            "path": f"{COMPOSE}/compose.env",
            "owner": "root",
            "group": "root",
            "mode": "0600",
            "keys": self.keys,
            "dependents": ["homelab-compose.service"],
            "format": "dotenv",
            **fields,
        }

    def edit(self, service, relative, text):
        """Replace one file of a fixture service, then rewrite the tree."""
        self.services[service] = {**self.services[service], relative: text}
        return self.write()

    def render(self):
        return host_mode.render_host(
            "daniel-pi", self.root, secrets=self.secrets, sops_dir=self.sops_dir
        )


def text(package, dst):
    return package.files[dst][0].decode()


def project_of(package):
    """The rendered project: `{"includes": [...], "services": {name: service}, "files": {...}}`."""
    top = yaml.safe_load(text(package, f"{COMPOSE}/compose.yaml"))
    services, files = {}, {}
    for include in top["include"]:
        body = yaml.safe_load(text(package, include["path"]))
        files[include["path"]] = body
        services |= body["services"]
    return {"includes": top["include"], "services": services, "files": files}


def project_problems(project):
    """Why the project breaks a rule `compose-run` or `compose-adopt` relies on."""
    problems = []
    for name, service in project["services"].items():
        depends = service.get("depends_on") or []
        for target in depends:
            if target not in project["services"]:
                problems.append(
                    f"{name} depends on {target}, which the project does not declare"
                )
        if service.get("container_name") != name:
            problems.append(
                f"{name} has container_name {service.get('container_name')!r}"
            )
        if "@sha256:" not in str(service.get("image")):
            problems.append(f"{name} image {service.get('image')!r} carries no digest")
    return problems


def tmpfiles_dirs(tmpfiles_text):
    """The directories the tmpfiles file lists with a `d` line."""
    return {
        line.split()[1]
        for line in tmpfiles_text.splitlines()
        if re.match(r"d\s+/", line)
    }


def bind_problems(project, listed):
    """Why a bind source is not a package path, a listed directory or the Docker socket."""
    problems, found = [], set()
    directory = {i["path"]: i["project_directory"] for i in project["includes"]}
    for path, body in project["files"].items():
        for name, service in body["services"].items():
            for volume in service.get("volumes") or []:
                if not isinstance(volume, dict) or volume.get("type") != "bind":
                    problems.append(
                        f"{name}: volume {volume!r} is not long-syntax bind"
                    )
                    continue
                source = volume["source"]
                if not (volume.get("bind") or {}).get("create_host_path") is False:
                    problems.append(f"{name}: {source} lacks create_host_path: false")
                resolved = (
                    str(PurePosixPath(directory[path]) / source.removeprefix("./"))
                    if source.startswith("./")
                    else source
                )
                found.add(resolved)
                allowed = (
                    resolved.startswith(f"{COMPOSE}/")
                    or resolved in listed
                    or resolved == "/var/run/docker.sock"
                )
                if resolved.startswith("/run/homelab-secrets/") or not allowed:
                    problems.append(f"{name}: bind source {resolved} is not allowed")
    return problems, found


def dependents_problems(package):
    """Why `secrets.json` does not name `homelab-compose.service` as compose.env's dependent."""
    manifest = json.loads(text(package, "/usr/lib/homelab/secrets.json"))
    rows = [
        o
        for o in manifest["outputs"]
        if o["destination"].endswith("compose/compose.env")
    ]
    if len(rows) != 1:
        return [f"{len(rows)} outputs render compose.env"]
    if "homelab-compose.service" not in rows[0]["dependents"]:
        return ["compose.env does not name homelab-compose.service as a dependent"]
    return []
