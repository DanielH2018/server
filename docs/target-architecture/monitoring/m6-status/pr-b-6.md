### PR B-6a and B-6b

B-6a ([#6689](https://github.com/DanielH2018/server/issues/6689)) wrote the `two_factor` rule
for `alertmanager.local` with `group:admins` as its subject, and the `deny` rule after it, in
`ansible/roles/k8s/authelia/templates/config-secret.yaml.j2`.

B-6b ([#6690](https://github.com/DanielH2018/server/issues/6690)) adds the UI route,
`ansible/roles/k8s/observability/templates/alertmanager-ingressroute.yaml.j2`, and sets
`--web.external-url=https://alertmanager.local.<domain>` on the Deployment. The route shares
its host with the GET-only read door from
[#6691](https://github.com/DanielH2018/server/issues/6691), which outranks it on the door's two
paths. Its verify-by read, a test silence created and expired through the UI, waits for the
promotion.
