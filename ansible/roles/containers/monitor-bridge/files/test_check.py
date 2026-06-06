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


if __name__ == "__main__":
    unittest.main()
