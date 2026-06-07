#!/usr/bin/env python3
"""Unit tests for the pure logic in check.py (timestamp parsing + backup-age).

Run: python3 -m unittest test_check   (from this directory)

Covers the parts that can be wrong without a live deploy noticing — chiefly the
nanosecond RFC3339 parsing (Kopia emits 9 fractional digits; fromisoformat caps at 6)
and the Kopia /api/v1/sources age/error extraction. The HTTP glue is exercised live
via `check.py --once` at deploy time.
"""
import unittest
from datetime import datetime, timezone
from unittest import mock

import check


class TestParseRFC3339(unittest.TestCase):
    def test_nanosecond_precision_with_z(self):
        # Real Kopia value: 9 fractional digits + trailing Z
        dt = check.parse_rfc3339("2026-06-06T00:00:00.011699074Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.microsecond, 11699)  # truncated from .011699074

    def test_plain_z_no_fraction(self):
        dt = check.parse_rfc3339("2026-06-06T00:00:00Z")
        self.assertEqual(dt, datetime(2026, 6, 6, tzinfo=timezone.utc))

    def test_offset_after_fraction(self):
        dt = check.parse_rfc3339("2026-06-06T01:00:00.123456789+01:00")
        self.assertEqual(dt.utcoffset().total_seconds(), 3600)
        self.assertEqual(dt.microsecond, 123456)


class TestBackupAge(unittest.TestCase):
    NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

    def _sources(self, last):
        return {
            "sources": [
                {"source": {"path": "/other"}, "lastSnapshot": {"startTime": "2020-01-01T00:00:00Z"}},
                {"source": {"path": "/data/containers"}, "lastSnapshot": last},
            ]
        }

    def test_age_from_endtime_and_errors(self):
        last = {
            "startTime": "2026-06-06T00:00:00.5Z",
            "endTime": "2026-06-06T06:00:00.011699074Z",
            "stats": {"errorCount": 0},
        }
        age, errs = check.backup_age_hours(self._sources(last), "/data/containers", now=self.NOW)
        self.assertAlmostEqual(age, 6.0, places=2)  # 06:00 -> 12:00
        self.assertEqual(errs, 0)

    def test_error_count_surfaced(self):
        last = {"endTime": "2026-06-06T11:00:00Z", "stats": {"errorCount": 3}}
        age, errs = check.backup_age_hours(self._sources(last), "/data/containers", now=self.NOW)
        self.assertEqual(errs, 3)
        self.assertAlmostEqual(age, 1.0, places=2)

    def test_missing_source_raises(self):
        with self.assertRaises(LookupError):
            check.backup_age_hours({"sources": []}, "/data/containers", now=self.NOW)

    def test_no_snapshot_raises(self):
        src = {"sources": [{"source": {"path": "/data/containers"}, "lastSnapshot": None}]}
        with self.assertRaises(LookupError):
            check.backup_age_hours(src, "/data/containers", now=self.NOW)


def _vector(*pairs):
    """Build a Prometheus instant-query JSON from (labels, value) pairs."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1700000000, str(val)]}
                for labels, val in pairs
            ],
        },
    }


class TestPromVector(unittest.TestCase):
    def test_parses_labels_and_values(self):
        payload = _vector(({"name": "sonarr"}, 5), ({"name": "radarr"}, 0))
        with mock.patch.object(check, "_get_json", return_value=payload):
            out = check.prom_vector("whatever")
        self.assertEqual(out, [({"name": "sonarr"}, 5.0), ({"name": "radarr"}, 0.0)])

    def test_empty_result_is_empty_list(self):
        with mock.patch.object(check, "_get_json", return_value=_vector()):
            self.assertEqual(check.prom_vector("q"), [])

    def test_non_success_raises(self):
        with mock.patch.object(check, "_get_json", return_value={"status": "error"}):
            with self.assertRaises(RuntimeError):
                check.prom_vector("q")


class TestCheckRestarts(unittest.TestCase):
    def test_names_containers_over_threshold(self):
        vec = [({"name": "sonarr"}, 5.0), ({"name": "radarr"}, 1.0)]
        with mock.patch.object(check, "prom_vector", return_value=vec):
            ok, msg = check.check_restarts()
        self.assertFalse(ok)
        self.assertIn("sonarr", msg)
        self.assertNotIn("radarr", msg)  # 1 restart is under the default max of 3

    def test_at_threshold_is_ok(self):
        # default RESTART_MAX=3; exactly 3 must NOT alert (strictly greater)
        vec = [({"name": "sonarr"}, 3.0)]
        with mock.patch.object(check, "prom_vector", return_value=vec):
            ok, _ = check.check_restarts()
        self.assertTrue(ok)

    def test_no_restarts_is_ok(self):
        with mock.patch.object(check, "prom_vector", return_value=[]):
            ok, _ = check.check_restarts()
        self.assertTrue(ok)


class TestCheckOom(unittest.TestCase):
    def test_names_oom_killed_container(self):
        vec = [({"name": "n8n"}, 2.0)]
        with mock.patch.object(check, "prom_vector", return_value=vec):
            ok, msg = check.check_oom()
        self.assertFalse(ok)
        self.assertIn("n8n", msg)

    def test_no_oom_is_ok(self):
        with mock.patch.object(check, "prom_vector", return_value=[]):
            ok, _ = check.check_oom()
        self.assertTrue(ok)


class TestCheckTargetsDown(unittest.TestCase):
    def test_names_down_target(self):
        vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 0.0)]
        with mock.patch.object(check, "prom_vector", return_value=vec):
            ok, msg = check.check_targets_down()
        self.assertFalse(ok)
        self.assertIn("cadvisor", msg)
        self.assertNotIn("node", msg)

    def test_all_up_is_ok(self):
        vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
        with mock.patch.object(check, "prom_vector", return_value=vec):
            ok, _ = check.check_targets_down()
        self.assertTrue(ok)


class TestCheckTraefik5xx(unittest.TestCase):
    def test_high_5xx_with_traffic_alerts(self):
        # total 1.0 rps, 0.2 rps of 5xx -> 20% > 5%
        with mock.patch.object(check, "prom_scalar", side_effect=[1.0, 0.2]):
            ok, msg = check.check_traefik_5xx()
        self.assertFalse(ok)
        self.assertIn("%", msg)

    def test_high_ratio_below_traffic_floor_is_ok(self):
        # 100% 5xx but only 0.01 rps (< 0.05 floor) -> must NOT alert
        with mock.patch.object(check, "prom_scalar", side_effect=[0.01, 0.01]):
            ok, _ = check.check_traefik_5xx()
        self.assertTrue(ok)

    def test_low_5xx_is_ok(self):
        with mock.patch.object(check, "prom_scalar", side_effect=[1.0, 0.01]):
            ok, _ = check.check_traefik_5xx()
        self.assertTrue(ok)

    def test_no_traffic_metric_is_ok(self):
        with mock.patch.object(check, "prom_scalar", side_effect=[None, None]):
            ok, _ = check.check_traefik_5xx()
        self.assertTrue(ok)


class TestCheckMemNoOom(unittest.TestCase):
    def test_reports_mem_pct_without_oom(self):
        # avail 2GB of 10GB -> 80% used, under default 90% -> ok, and no OOM wording
        with mock.patch.object(check, "prom_scalar", side_effect=[2e9, 10e9]) as ps:
            ok, msg = check.check_mem()
        self.assertTrue(ok)
        self.assertNotIn("OOM", msg)
        self.assertEqual(ps.call_count, 2)  # only mem queries, no OOM query

    def test_high_mem_alerts(self):
        with mock.patch.object(check, "prom_scalar", side_effect=[0.5e9, 10e9]):
            ok, msg = check.check_mem()
        self.assertFalse(ok)
        self.assertIn("mem", msg.lower())


if __name__ == "__main__":
    unittest.main()
