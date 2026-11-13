"""The dialect's spec fixtures for constructs: one literal per filter, test and lookup.

The PR that added them checked each literal once against ansible-core, so they need no oracle.
"""

from pathlib import Path
from typing import NamedTuple

FIXTURES = Path(__file__).parent / "fixtures" / "dialect"

ENTRIES = {
    "containers_list": [
        {
            "name": "app",
            "port": 8080,
            "hostname": "app",
            "tier": "platform",
            "use_authelia": True,
            "auth_tier": "one_factor",
            "homepage_widget": True,
            "platform": "k8s",
            "netpol_from": ["caller"],
            "metrics": [{"job": "app-metrics", "port": 9100}],
            "backup_claims": ["app-data"],
            "weekly_backup_claims": {"app-weekly": 3},
            "no_backup_claims": ["app-cache"],
        }
    ]
}


class Case(NamedTuple):
    """One construct's spec.

    Attributes:
        kind: `filter`, `test` or `lookup`.
        name: The construct's name.
        template: The template text. It reads `lookup_dir`, the fixtures' directory, by name.
        variables: The variables it renders with.
        expected: The text it renders to.
    """

    kind: str
    name: str
    template: str
    variables: dict
    expected: str


CASES = (
    Case(
        "filter",
        "authelia_service_rules",
        "{{ containers_list | authelia_service_rules('example.com', ['10.0.0.0/8']) | to_json }}",
        ENTRIES,
        '[{"domain": ["app.local.example.com"], "policy": "one_factor", "networks": ["10.0.0.0/8"]}]',
    ),
    Case("filter", "basename", "{{ '/a/b.txt' | basename }}", {}, "b.txt"),
    Case(
        "filter",
        "bool",
        "{{ 'false' | bool }} {{ 'yes' | bool }} {{ 0 | bool }}",
        {},
        "False True False",
    ),
    Case("filter", "capitalize", "{{ 'hello World' | capitalize }}", {}, "Hello world"),
    Case(
        "filter",
        "combine",
        "{{ {'a': 1, 'b': 1} | combine({'b': 2}) | to_json }}",
        {},
        '{"a": 1, "b": 2}',
    ),
    Case(
        "filter",
        "default",
        "{{ absent | default('x') }} {{ '' | default('y', true) }}",
        {},
        "x y",
    ),
    Case(
        "filter",
        "dictsort",
        "{{ {'b': 1, 'a': 2} | dictsort | to_json }}",
        {},
        '[["a", 2], ["b", 1]]',
    ),
    Case(
        "filter",
        "entry_names",
        "{{ containers_list | entry_names | to_json }}",
        ENTRIES,
        '["app"]',
    ),
    Case(
        "filter",
        "entry_port",
        "{{ containers_list | entry_port('app') }}",
        ENTRIES,
        "8080",
    ),
    Case("filter", "first", "{{ [3, 4] | first }}", {}, "3"),
    Case(
        "filter",
        "flux_components",
        "{{ install | flux_components({'spec': {'template': {'spec': {'nodeSelector': {'a': 'b'}}}}}, 'high', {}) }}",
        {
            "install": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: ctl\nspec:\n  template:\n    spec:\n      containers:\n        - name: ctl\n          image: x\n---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: flux\n"
        },
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: ctl\nspec:\n  template:\n    spec:\n      containers:\n        - name: ctl\n          image: x\n      nodeSelector:\n        a: b\n      priorityClassName: high\n  strategy:\n    type: RollingUpdate\n---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: flux\n",
    ),
    Case("filter", "format", "{{ '%s-%d' | format('a', 1) }}", {}, "a-1"),
    Case(
        "filter",
        "from_yaml",
        "{{ ('a: [1, 2]' | from_yaml).a | to_json }}",
        {},
        "[1, 2]",
    ),
    Case(
        "filter",
        "grafana_deploy_annotated",
        "{{ board | grafana_deploy_annotated('uid-1', 'expr-a', 'expr-b') }}",
        {"board": '{"b": 1, "annotations": {"list": []}, "a": 2}'},
        '{\n  "a": 2,\n  "annotations": {\n    "list": [\n      {\n        "datasource": {\n          "type": "loki",\n          "uid": "uid-1"\n        },\n        "enable": true,\n        "hide": false,\n        "iconColor": "purple",\n        "name": "Deploys",\n        "target": {\n          "expr": "expr-a",\n          "queryType": "range",\n          "refId": "DeployAnno"\n        },\n        "textFormat": "{{services}}",\n        "titleFormat": "deploy {{result}}"\n      },\n      {\n        "datasource": {\n          "type": "loki",\n          "uid": "uid-1"\n        },\n        "enable": true,\n        "hide": false,\n        "iconColor": "purple",\n        "name": "Flux deploys",\n        "target": {\n          "expr": "expr-b",\n          "queryType": "range",\n          "refId": "FluxDeployAnno"\n        },\n        "textFormat": "{{services}}",\n        "titleFormat": "deploy {{result}}"\n      }\n    ]\n  },\n  "b": 1\n}\n',
    ),
    Case(
        "filter",
        "hash",
        "{{ 'abc' | hash('sha256') }}",
        {},
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    ),
    Case(
        "filter",
        "homelab_role_label",
        "{{ 'kind: Service\nmetadata:\n  name: x\n' | homelab_role_label('app') }}",
        {},
        "kind: Service\nmetadata:\n  name: x\n  labels:\n    homelab/role: app\n",
    ),
    Case(
        "filter",
        "homepage_href",
        "{{ containers_list | homepage_href('app', 'example.com', '/x', lan=true) }}",
        ENTRIES,
        "https://app.local.example.com/x",
    ),
    Case(
        "filter",
        "homepage_widget_url",
        "{{ containers_list | homepage_widget_url('app', 'homelab') }}",
        ENTRIES,
        "http://app.homelab.svc.cluster.local:8080",
    ),
    Case(
        "filter",
        "in_service_tier",
        "{{ containers_list | in_service_tier('app', 'platform') }}",
        ENTRIES,
        "True",
    ),
    Case(
        "filter",
        "indent",
        "{{ 'a\nb\n\nc' | indent(2) }}|{{ 'a\nb' | indent(2, first=true) }}",
        {},
        "a\n  b\n\n  c|  a\n  b",
    ),
    Case(
        "filter",
        "ingress_probe_targets",
        "{{ containers_list | ingress_probe_targets | to_json }}",
        ENTRIES,
        '[{"hostname": "app", "behind_sso": true, "entry": "app"}]',
    ),
    Case("filter", "int", "{{ '12' | int + 1 }} {{ 'x' | int(7) }}", {}, "13 7"),
    Case("filter", "join", "{{ [1, 'a'] | join(',') }}", {}, "1,a"),
    Case(
        "filter",
        "lan_hostnames",
        "{{ containers_list | lan_hostnames | to_json }}",
        ENTRIES,
        '["app"]',
    ),
    Case("filter", "length", "{{ [1, 2] | length }} {{ 'abc' | length }}", {}, "2 3"),
    Case("filter", "list", "{{ 'ab' | list | to_json }}", {}, '["a", "b"]'),
    Case(
        "filter",
        "longhorn_backup_name",
        "{{ 'r2_target' | longhorn_backup_name }}",
        {},
        "r2",
    ),
    Case(
        "filter",
        "longhorn_nobackup_claims",
        "{{ containers_list | longhorn_nobackup_claims('homelab') | to_json }}",
        ENTRIES,
        '["homelab/app-cache"]',
    ),
    Case(
        "filter",
        "longhorn_weekly_claims",
        "{{ containers_list | longhorn_weekly_claims('homelab') | to_json }}",
        ENTRIES,
        '{"homelab/app-weekly": 3}',
    ),
    Case("filter", "lower", "{{ 'AbC' | lower }}", {}, "abc"),
    Case(
        "filter",
        "map",
        "{{ ['a', 'b'] | map('upper') | list | to_json }} {{ [{'n': 1}] | map(attribute='n') | list | to_json }}",
        {},
        '["A", "B"] [1]',
    ),
    Case("filter", "max", "{{ [1, 3, 2] | max }}", {}, "3"),
    Case(
        "filter",
        "metrics_port",
        "{{ containers_list | metrics_port('app-metrics') }}",
        ENTRIES,
        "9100",
    ),
    Case("filter", "min", "{{ [2, 1, 3] | min }}", {}, "1"),
    Case(
        "filter",
        "netpol_caller_fences",
        "{{ containers_list | netpol_caller_fences | to_json }}",
        ENTRIES,
        '[{"app": "app", "port": 8080, "callers": ["caller", "homepage"]}]',
    ),
    Case(
        "filter",
        "netpol_callers",
        "{{ containers_list[0] | netpol_callers | to_json }}",
        ENTRIES,
        '["caller", "homepage"]',
    ),
    Case(
        "filter",
        "published_ports",
        "{{ containers_list | published_ports | to_json }}",
        ENTRIES,
        '["app:8080"]',
    ),
    Case(
        "filter",
        "reject",
        "{{ ['a', 'b:c'] | reject('search', ':') | list | to_json }}",
        {},
        '["a"]',
    ),
    Case("filter", "replace", "{{ 'a-b-c' | replace('-', '_', 1) }}", {}, "a_b-c"),
    Case(
        "filter",
        "reverse",
        "{{ [1, 2] | reverse | list | to_json }} {{ 'ab' | reverse }}",
        {},
        "[2, 1] ba",
    ),
    Case(
        "filter",
        "scrape_jobs",
        "{{ containers_list | scrape_jobs('homelab') | to_json }}",
        ENTRIES,
        '[{"job": "app-metrics", "target": "app.homelab.svc:9100"}]',
    ),
    Case(
        "filter",
        "selectattr",
        "{{ [{'a': 1, 'b': 2}, {'b': 3}] | selectattr('a', 'defined') | map(attribute='b') | list | to_json }}",
        {},
        "[2]",
    ),
    Case(
        "filter",
        "sort",
        "{{ [3, 1, 2] | sort | to_json }} {{ [{'k': 2}, {'k': 1}] | sort(attribute='k') | to_json }}",
        {},
        '[1, 2, 3] [{"k": 1}, {"k": 2}]',
    ),
    Case("filter", "string", "{{ 1 | string }}{{ true | string }}", {}, "1True"),
    Case(
        "filter",
        "tier_backup_claims",
        "{{ containers_list | tier_backup_claims('platform', 'homelab') | to_json }}",
        ENTRIES,
        '["homelab/app-data"]',
    ),
    Case(
        "filter",
        "tier_priority_class",
        "{{ containers_list | tier_priority_class('app') }}",
        ENTRIES,
        "homelab-critical",
    ),
    Case(
        "filter",
        "to_json",
        "{{ {'b': 'é<', 'a': [1, true, none]} | to_json }}",
        {},
        '{"b": "\\u00e9<", "a": [1, true, null]}',
    ),
    Case(
        "filter",
        "tojson",
        "{{ {'b': 'é<', 'a': [1, true, none]} | tojson }}",
        {},
        '{"a": [1, true, null], "b": "\\u00e9\\u003c"}',
    ),
    Case("filter", "trim", "{{ '  a  ' | trim }}", {}, "a"),
    Case("filter", "unique", "{{ [1, 2, 1] | unique | list | to_json }}", {}, "[1, 2]"),
    Case("filter", "upper", "{{ 'aB' | upper }}", {}, "AB"),
    Case("filter", "urlencode", "{{ 'a b/c' | urlencode }}", {}, "a%20b/c"),
    Case(
        "filter",
        "weekly_backup_group",
        "{{ 3 | weekly_backup_group }}",
        {},
        "weekly-backup-d3",
    ),
    Case(
        "test",
        "defined",
        "{{ absent is defined }} {{ 1 is defined }}",
        {},
        "False True",
    ),
    Case(
        "test",
        "equalto",
        "{{ [{'a': 1}, {'a': 2}] | selectattr('a', 'equalto', 2) | map(attribute='a') | list | to_json }}",
        {},
        "[2]",
    ),
    Case("test", "in", "{{ 1 is in [1, 2] }} {{ 3 is in [1, 2] }}", {}, "True False"),
    Case("test", "none", "{{ none is none }} {{ 0 is none }}", {}, "True False"),
    Case(
        "test",
        "search",
        "{{ 'abc' is search('b') }} {{ 'abc' is search('^b') }}",
        {},
        "True False",
    ),
    Case(
        "lookup",
        "file",
        "[{{ lookup('file', lookup_dir ~ '/text.txt') }}]",
        {},
        "[plain line]",
    ),
    Case(
        "lookup",
        "b64file",
        "{{ lookup('b64file', lookup_dir ~ '/text.txt') }}",
        {},
        "cGxhaW4gbGluZQo=",
    ),
    Case(
        "lookup",
        "template",
        "[{{ lookup('template', lookup_dir ~ '/inner.j2') }}]",
        {"value": "v"},
        "[inner v\n]",
    ),
    Case("lookup", "vars", "{{ lookup('vars', 'value') }}", {"value": "v"}, "v"),
)
