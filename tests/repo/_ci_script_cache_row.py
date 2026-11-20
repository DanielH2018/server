"""The census row holding that a CI step running a downloading script restores its cache first."""

import re

from _row_table import Census, Subject, tracked
from _workflow_rows import (
    CI,
    Workflow,
    jobs,
    judge,
    mutants,
    parsed,
    runs,
    steps,
    uses,
)

# The scripts that fetch into a cache directory of their own, and that directory.
SCRIPT_CACHES = {
    "scripts/validate/alert_tools.sh": ".git/alert-tools",
    "scripts/validate/vale.sh": "styles/Google",
    # Provisions Alloy through alert_tools.sh, keeping the zip in ALERT_TOOLS_ARCHIVES.
    "scripts/validate/alert_rules.py --prove-red": "$RUNNER_TEMP/pinned/alloy",
}
PIN_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:env\.|steps\.|hashFiles\()")


def _cache_paths(step: dict) -> list[str]:
    raw = str((step.get("with") or {}).get("path", ""))
    return [
        line.strip().replace("${{ runner.temp }}", "$RUNNER_TEMP").removesuffix("/*")
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("!")
    ]


def _uncached_script_downloads(wf: Workflow) -> list[str]:
    found = []
    for job_name, job in jobs(wf).items():
        cached: dict[str, str] = {}
        for step in steps(job):
            if uses(step, "actions/cache"):
                key = str((step.get("with") or {}).get("key", ""))
                cached |= dict.fromkeys(_cache_paths(step), key)
                continue
            for script, path in SCRIPT_CACHES.items():
                if script not in runs(step):
                    continue
                where = f"{job_name}: {step.get('name')}"
                if path not in cached:
                    found.append(
                        f"{where}: no earlier actions/cache step caches {path}"
                    )
                elif not PIN_EXPRESSION.search(cached[path]):
                    found.append(
                        f"{where}: the cache key of {path} does not read a pin"
                    )
    return found


def _drop_cache(name: str):
    def mutate(wf: Workflow) -> None:
        hooks = wf["jobs"]["hooks"]
        hooks["steps"] = [s for s in steps(hooks) if s.get("name") != name]

    return mutate


def _constant_alloy_key(wf: Workflow) -> None:
    for step in steps(wf["jobs"]["hooks"]):
        if step.get("name") == "Cache the Alloy release zip":
            step["with"]["key"] = "alloy-zip"


def _script_count(subject: Subject) -> int:
    return sum(
        script in runs(step)
        for job in jobs(parsed(subject)).values()
        for step in steps(job)
        for script in SCRIPT_CACHES
    )


SCRIPT_DOWNLOADS_ARE_CACHED = Census(
    name="a-downloading-script-runs-after-its-cache",
    reason=(
        "A script that fetches release archives into its own directory outlasts a "
        "release-host outage only when an earlier actions/cache step restores that "
        "directory under a key that reads the pin, so a run downloads only when the pin "
        "moves. `.github/actions/pinned-tool` holds a binary fetched by the workflow "
        "itself to the same rule."
    ),
    files=lambda: tracked(CI),
    offence=judge(_uncached_script_downloads),
    count=_script_count,
    red=mutants(
        CI,
        no_alert_tools_cache=_drop_cache("Cache promtool, amtool, pint and lokitool"),
        no_vale_styles_cache=_drop_cache("Cache the Vale style package"),
        no_alloy_cache=_drop_cache("Cache the Alloy release zip"),
        constant_alloy_key=_constant_alloy_key,
    ),
    green=(Subject(CI),),
    min_matches=3,
    must_find=frozenset({CI}),
)
