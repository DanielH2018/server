// pint's offline config for the observability role's rule files. scripts/validate/alert_rules.py
// runs `pint --offline lint` with it on every commit the validate-alert-rules hook covers, and
// `alert_rules.py --prove-red` shows it rejecting
// scripts/validate/tests/fixtures/alert_rules/bad-pint-rules.yaml.
//
// Every alert carries a `severity` from the three values Alertmanager routes on. A `bug` fails
// `pint lint`; its default `--fail-on` is bug, and a warning prints without failing.
rule {
  match {
    kind = "alerting"
  }
  label "severity" {
    severity = "bug"
    value    = "(critical|warning|info)"
    required = true
  }
}
