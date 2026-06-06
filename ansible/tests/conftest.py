"""Put ansible/filter_plugins on sys.path so the toposort tests can import it.

These tests live OUTSIDE filter_plugins/ on purpose: Ansible's filter-plugin loader scans
that directory *recursively* and imports every .py as a plugin, so a pytest test placed
anywhere under filter_plugins/ fails to load at deploy time ("No module named 'pytest'").
"""
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "filter_plugins"),
)
