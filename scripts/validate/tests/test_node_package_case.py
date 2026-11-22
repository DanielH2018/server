"""The cluster-node package container case's verdict and seed, without a container.

The case itself runs only in CI's `host_packages` job, once per Ubuntu release.

Run: uv run pytest scripts/validate/tests/test_node_package_case.py
"""

from renderer.renderer_lib import host as host_mode
from validate import host_package_cases
from validate.validate_lib import case_groups, node_package_case
from validate.validate_lib.node_package_case import (
    NODES,
    STUBBED_UNITS,
    failed_units,
)


def test_an_empty_failed_listing_is_clean():
    assert failed_units("") == []


def test_each_failed_unit_is_flagged_by_name():
    listing = (
        "homelab-reconcile.service loaded failed failed Reconcile the homelab package\n"
        "nut-monitor.service       loaded failed failed Network UPS Tools - power device monitor\n"
    )
    assert failed_units(listing) == ["homelab-reconcile.service", "nut-monitor.service"]


def test_every_stubbed_unit_is_one_a_node_package_ships():
    shipped = {unit for host in NODES for unit in host_mode.render_host(host).units()}
    assert set(STUBBED_UNITS) - shipped == set()


def test_the_host_packages_job_runs_the_case():
    runs = [run for _, run in case_groups.groups(host_package_cases.Cases)]
    assert node_package_case.run in runs
