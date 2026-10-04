"""The volumes the daily RecurringJob backs up are exactly `k3s_longhorn_r2_volumes`.

Two mechanisms decide a volume's backup cadence and target, and neither names the other. The
daily job selects group `default` (`templates/longhorn-recurringjob.yaml.j2`); the label
reconcile in `tasks/longhorn.yml` moves the weekly and no-backup lists OUT of `default` and
returns everything else to it, so daily membership is "every `longhorn`-class PVC in neither
list". The r2 list, read by `tasks/longhorn-backup.yml`, decides only which BackupTarget a
volume points at. Today the two sets are identical: the daily tier is the R2 tier and the
weekly tier is the B2 tier (`docs/longhorn-backup-tiering.md`). A PVC added to the tree and
routed into no list, or listed under r2 AND weekly, would split them — daily backups landing
on B2's 2,500/day transaction cap is the sixth cap event again. The disjointness and
every-PVC-is-routed halves are asserted by their siblings here; this asserts the identity
that follows from them, and the two structural facts it rests on.

Run: uv run pytest ansible/tests/longhorn/test_daily_group_membership_is_the_r2_list.py
"""

from _helpers import SETUP_ROLES, load_defaults, load_tasks, task_named
from _setup_render import rendered_setup_text
from lib import yaml_fast
from lib.service_tiers import resolved_tier_lists
from test_every_longhorn_pvc_has_a_tier import _longhorn_class_pvcs

K3S = SETUP_ROLES / "k3s"
RECURRING_JOB = "longhorn-recurringjob.yaml.j2"
LEAVES_DEFAULT = ("k3s_longhorn_nobackup_volumes", "k3s_longhorn_weekly_volumes")


def recurring_jobs() -> dict[str, dict]:
    """Every RecurringJob the k3s role applies, by `metadata.name`, out of the RENDER.

    Read from `_setup_render` rather than from the template's text: the cron and the retain
    counts are role defaults, and the weekly tier's seven shards only exist after the
    `{% for %}` runs, so the source carries neither the group names nor the job count.
    """
    docs = yaml_fast.safe_load_all(rendered_setup_text("k3s", RECURRING_JOB))
    return {d["metadata"]["name"]: d for d in docs if d}


def daily_members(declared: set[str], defaults: dict) -> set[str]:
    """Who ends up in group `default` after the reconcile: every PVC not moved out of it."""
    return declared - {v for name in LEAVES_DEFAULT for v in defaults[name]}


def test_daily_group_membership_equals_the_r2_list():
    defaults = resolved_tier_lists(load_defaults(K3S))
    declared = _longhorn_class_pvcs()
    assert len(declared) >= 4
    assert daily_members(declared, defaults) == set(defaults["k3s_longhorn_r2_volumes"])


def test_the_daily_job_selects_the_default_group():
    jobs = recurring_jobs()
    assert jobs["daily-backup"]["spec"]["groups"] == ["default"], (
        "the reconcile below assumes the `default` group"
    )


def test_no_other_recurring_job_claims_the_default_group():
    """Non-vacuity, and the other half of the identity: `default` has ONE claimant.

    The seven `weekly-backup-d<N>` shards are the members this must find. A second job on
    `default` would back every unlisted volume up twice — the daily tier's volumes are the R2
    tier, so the duplicate would be the one landing on B2's transaction cap.
    """
    jobs = recurring_jobs()
    weekly = sorted(name for name in jobs if name.startswith("weekly-backup-d"))
    assert weekly == [f"weekly-backup-d{shard}" for shard in range(7)], weekly
    on_default = sorted(
        name for name, job in jobs.items() if "default" in job["spec"]["groups"]
    )
    assert on_default == ["daily-backup"], on_default


def test_the_reconcile_returns_only_the_unlisted_volumes_to_default():
    task = task_named(
        load_tasks(K3S / "tasks" / "longhorn.yml"), "Return volumes to the default"
    )
    conditions = task["when"]
    for name in LEAVES_DEFAULT:
        assert f"not in {name}" in " ".join(conditions), name
    assert "k3s_longhorn_r2_volumes" not in " ".join(conditions), (
        "the r2 list decides the target, not the group; keying the group on it would let a "
        "volume in no list fall out of every backup"
    )


def test_a_pvc_in_no_list_and_a_pvc_in_two_lists_both_break_the_identity():
    defaults = {
        "k3s_longhorn_r2_volumes": ["ns/a"],
        "k3s_longhorn_weekly_volumes": ["ns/b"],
        "k3s_longhorn_nobackup_volumes": ["ns/c"],
    }
    assert daily_members({"ns/a", "ns/b", "ns/c"}, defaults) == {"ns/a"}
    assert daily_members({"ns/a", "ns/b", "ns/c", "ns/d"}, defaults) == {"ns/a", "ns/d"}
    defaults["k3s_longhorn_weekly_volumes"].append("ns/a")
    assert daily_members({"ns/a", "ns/b", "ns/c"}, defaults) == set()
