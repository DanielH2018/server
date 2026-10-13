// pint's live config, run by hand because CI cannot reach the cluster. A rule PR records a clean
// run of it against the live Prometheus:
//
//   scripts/validate/alert_tools.sh pint --no-color --config scripts/validate/pint/live.hcl \
//     lint ansible/roles/k8s/observability/files/rules/
//
// pint reports a metric Prometheus has never held as a `promql/series` bug, which fails the run.
//
// The address is the Prometheus Service's pinned ClusterIP, observability_prometheus_cluster_ip
// in the observability role's defaults, because it answers from either node. The loopback
// hostPort 127.0.0.1:9090 answers only on the node the pod runs on.
//
// `required = true` makes an unreachable Prometheus a `bug`. With the default, false, pint
// reports it as a warning and exits 0, so a clean run would not show that Prometheus answered.
prometheus "cluster" {
  uri      = "http://10.43.39.218:9090"
  timeout  = "60s"
  required = true
}
