"""Ablate a CLAUDE.md doc's sections against the homelab eval cases (#4259).

A hermetic eval run loads no CLAUDE.md at all: the engine passes `--bare` and
`--setting-sources project`, so an agent under test sees its own body and the case input
and nothing else. To measure whether a doc section changes an outcome, this script puts the
doc there itself. For every arm it writes a copy of each case's agent (or skill) file to a
temp directory outside the checkout, with the doc appended to the body, and points the
chezmoi engine at that directory through EVAL_AGENT_DIRS. The engine is unchanged.

Arms, run in this order:
  baseline   the agent body plus the whole doc
  -doc       the agent body alone, which ablates the whole doc
  -<heading> the agent body plus the doc with that one `## ` section removed

Every arm uses the same framing around the doc, so the removed text is the only difference
between an ablated arm and the baseline. The engine's rules loader (load-rules.mjs) holds
its A/B arms to the same rule.

THE BUDGET IS CHECKED BETWEEN RUNS, because that is the finest point an outside caller has:
run-evals.mjs writes its --json report only when its invocation ends. So each invocation is
one case at --k 1, and before each one the runner stops if the spend so far plus a
projected next run would cross the cap. The projection is never below WORST_RUN_USD, the
most one call can bill under the engine's per-call --max-budget-usd, so a run that launches
cannot carry the total past the cap. It rises above that floor only when a run has cost
more, at the costliest run seen so far times PROJECTION_MARGIN. A report with no costUsd,
or no report at all, stops the run, because its spend is unknown.

THE PLAN IS PRICED BEFORE IT STARTS (#4308). The floor above leaves about $6.25 of each $10
spendable, and the baseline arm of a 30-case plan at k=3 costs more than that, so a plan that
cannot finish the baseline, -doc and one section arm spends money and measures nothing. `run`
prices each case from past engine reports and refuses such a plan, dry run included, and names
the largest selection that fits.

The report goes to --out, never to evals/history.json: an ablation arm is not a sweep, and
trend.py would mix it into the regression baseline.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import shlex
import tempfile
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from ablate_budget import (
    ABLATION_DIR,
    CAP_USD,
    SWEEP_ARCHIVE,
    UNPRICED_RUN_USD,
    WORST_RUN_USD,
    Budget,
    complete_arms,
    fitting_selection,
    report_entries,
    run_costs,
)
from ranking import default_transcripts, rank_docs, section_reads

REPO = Path(__file__).resolve().parent.parent
CASES_DIR = REPO / "evals" / "cases"
HISTORY = REPO / "evals" / "history.json"
CHEZMOI = Path.home() / ".local" / "share" / "chezmoi"
ENGINE = CHEZMOI / "evals" / "run-evals.mjs"
# The engine searches its own agents dir BEFORE EVAL_AGENT_DIRS, so a homelab agent sharing
# a name with one there would be graded without the doc and every arm would read identical.
CHEZMOI_AGENTS = CHEZMOI / "home" / "private_dot_claude" / "agents"

# The weekly sweep's day, as Python's date.weekday() numbers it (Monday is 0). The cron in
# ansible/roles/setup/initial_setup/tasks/crons.yml says weekday "0", cron's Sunday; a test
# holds the two together. An ablation run must not share a day with the sweep, because both
# spend the same monthly credit.
SWEEP_WEEKDAY = 6

BASELINE = "baseline"
WHOLE_DOC = "-doc"

EXIT_DONE = 0
EXIT_REFUSED = 2
EXIT_CAPPED = 3

_FRONTMATTER = re.compile(r"\A(---\n.*?\n---\n?)(.*)\Z", re.S)
_FENCE = re.compile(r"^(```|~~~)")


def split_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a doc into its preamble and its `## ` sections, in doc order.

    Each section's text runs from its heading line up to the next `## ` heading, so it
    carries its `###` subsections with it. A `## ` line inside a code fence is not a heading.
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in md.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "".join(preamble), [(h, "".join(body)) for h, body in sections]


def doc_without(md: str, heading: str) -> str:
    preamble, sections = split_sections(md)
    if heading not in [h for h, _ in sections]:
        raise KeyError(f"no '## {heading}' section")
    return preamble + "".join(text for h, text in sections if h != heading)


def agent_source(name: str, repo: Path = REPO) -> Path:
    """Where the engine would find `name`: a flat agent file, else a skill directory."""
    for path in (
        repo / ".claude" / "agents" / f"{name}.md",
        repo / ".claude" / "skills" / name / "SKILL.md",
    ):
        if path.is_file():
            return path
    raise FileNotFoundError(f"no agent or skill named {name!r} under {repo}/.claude")


def with_doc(agent_md: str, doc_label: str, doc_text: str | None) -> str:
    """The agent file with the doc appended to its body; unchanged when doc_text is None.

    The framing copies how a live session introduces a project CLAUDE.md, so the agent
    reads the doc the way a dispatched subagent would.
    """
    if doc_text is None:
        return agent_md
    m = _FRONTMATTER.match(agent_md)
    if not m:
        raise ValueError("agent file has no YAML frontmatter")
    head, body = m.groups()
    return (
        f"{head}{body.rstrip()}\n\n"
        f"Contents of {doc_label} (project instructions, checked into the codebase):\n\n"
        f"{doc_text.rstrip()}\n"
    )


def _local_date(ts: float) -> date:
    # Local, like the cron that schedules the sweep.
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().date()


def sweep_day_conflict(today: date, history: dict) -> str | None:
    """Why `today` belongs to the weekly sweep, or None when an ablation may run."""
    if today.weekday() == SWEEP_WEEKDAY:
        return f"{today} is the weekly eval sweep's day"
    for run in history.get("_runs", []):
        if _local_date(run.get("ts", 0)) == today:
            return f"evals/history.json records a sweep on {today}"
    return None


def load_cases(
    case_ids: list[str], agents: list[str], cases_dir: Path = CASES_DIR
) -> list[dict]:
    found = [json.loads(p.read_text()) for p in sorted(cases_dir.glob("*/*.json"))]
    picked = [
        c
        for c in found
        # The engine's hermetic runner skips live cases, so one would write no report.
        if c.get("mode") != "live"
        and (not case_ids or c["id"] in case_ids)
        and (not agents or c["agent"] in agents)
    ]
    missing = set(case_ids) - {c["id"] for c in picked}
    if missing:
        raise KeyError(f"unknown or live case id(s): {', '.join(sorted(missing))}")
    return picked


Invoke = Callable[[str, dict, Path], dict | None]


def ablate(
    arms: list[tuple[str, dict[str, str]]],
    cases: list[dict],
    k: int,
    budget: Budget,
    invoke: Invoke,
    agent_root: Path,
    refuse: Callable[[], str | None] = lambda: None,
) -> dict:
    """Run every case k times per arm until the arms run out or the budget stops it.

    `arms` pairs an arm name with {agent name: agent file text}. `invoke(case_id, agent_dirs
    env, run_dir)` runs one case once and returns that run's case report, or None when the
    engine wrote none. `refuse()` names a reason not to launch, re-checked before every run.
    The result maps each arm to {case id: {passes, healthy, costUsd}}, and names the arm a
    stop cut short.
    """
    results: dict[str, dict[str, dict]] = {}
    stopped = None
    for arm, agent_files in arms:
        arm_dir = agent_root / f"arm-{len(results):03d}"
        arm_dir.mkdir(parents=True)
        for name, text in agent_files.items():
            (arm_dir / f"{name}.md").write_text(text)
        env = {"EVAL_AGENT_DIRS": str(arm_dir), "EVAL_CASE_DIRS": str(CASES_DIR)}
        results[arm] = {}
        for case in cases:
            tally = results[arm].setdefault(
                case["id"], {"passes": 0, "healthy": 0, "costUsd": 0.0}
            )
            for _ in range(k):
                if not budget.next_run_fits():
                    stopped = (
                        f"next run projected at ${budget.projected():.2f} would take "
                        f"${budget.spent:.2f} past the ${budget.cap:.2f} cap"
                    )
                    return {"results": results, "stopped": stopped, "cut_arm": arm}
                stopped = refuse()
                if stopped:
                    return {"results": results, "stopped": stopped, "cut_arm": arm}
                report = invoke(case["id"], env, arm_dir)
                if report is None:
                    # The run may have billed calls before it died, and no report says how
                    # much, so the cap can no longer be enforced.
                    stopped = (
                        f"the engine wrote no report for {case['id']}; spend unknown"
                    )
                    return {"results": results, "stopped": stopped, "cut_arm": arm}
                cost = report.get("costUsd")
                if cost is None:
                    # An engine from before calls were priced; counting it as $0 would
                    # disable the cap for the whole run.
                    stopped = (
                        f"the report for {case['id']} carries no costUsd; spend unknown"
                    )
                    return {"results": results, "stopped": stopped, "cut_arm": arm}
                budget.add(cost)
                tally["passes"] += report.get("passes", 0)
                tally["healthy"] += report.get("healthy", 0)
                tally["costUsd"] += cost
                if budget.crossed():
                    stopped = (
                        f"spent ${budget.spent:.2f}, past the ${budget.cap:.2f} cap"
                    )
                    return {"results": results, "stopped": stopped, "cut_arm": arm}
    return {"results": results, "stopped": None, "cut_arm": None}


def threshold_met(threshold: str, tally: dict) -> bool:
    """Whether a case's tally meets its `threshold`, as the engine's report.mjs reads it."""
    passes, healthy = tally["passes"], tally["healthy"]
    if threshold == "all":
        return healthy > 0 and passes == healthy
    m = re.fullmatch(r"rate>=(\d+)/(\d+)", threshold)
    if not m:
        raise ValueError(f"bad threshold: {threshold}")
    rate = passes / healthy if healthy else 0.0
    return rate >= int(m.group(1)) / int(m.group(2))


def summarize(run: dict, arm_names: list[str], cases: list[dict], k: int) -> dict:
    """Per ablated arm: each case's outcome with and without, and whether any moved.

    An arm counts as measured only when every case ran k times in it and in the baseline.
    The cap-cut arm and the arms after it are listed as not measured.
    """
    results = run["results"]

    def complete(arm: str) -> bool:
        return arm in results and arm != run["cut_arm"]

    base = results.get(BASELINE, {})
    measured, unmeasured = {}, []
    for arm in arm_names:
        if arm == BASELINE:
            continue
        if not (complete(BASELINE) and complete(arm)):
            unmeasured.append(arm)
            continue
        rows = {}
        for case in cases:
            with_it, without = base[case["id"]], results[arm][case["id"]]
            # An infra error leaves a run unhealthy, and 1/1 against 0/0 is not a flip.
            inconclusive = with_it["healthy"] < k or without["healthy"] < k
            met_with = threshold_met(case["threshold"], with_it)
            met_without = threshold_met(case["threshold"], without)
            rows[case["id"]] = {
                "with": f"{with_it['passes']}/{with_it['healthy']}",
                "without": f"{without['passes']}/{without['healthy']}",
                # The case's own threshold decides, as it does in a sweep, so a 2/3 case
                # moving to 3/3 is not a change and a single noisy run is less likely to be.
                "changed": not inconclusive and met_with != met_without,
                "inconclusive": inconclusive,
            }
        measured[arm] = {
            "changed": any(r["changed"] for r in rows.values()),
            "inconclusive": [cid for cid, r in rows.items() if r["inconclusive"]],
            "cases": rows,
        }
    return {"k": k, "measured": measured, "unmeasured": unmeasured}


def engine_invoke(engine: Path, node: str, out_dir: Path) -> Invoke:
    """One `run-evals.mjs --case <id> --k 1` call, returning that case's report entry."""
    counter = iter(range(1_000_000))

    def invoke(case_id: str, env: dict, arm_dir: Path) -> dict | None:
        report_path = out_dir / f"{arm_dir.name}-{next(counter):05d}.json"
        subprocess.run(
            [
                node,
                str(engine),
                "--case",
                case_id,
                "--k",
                "1",
                "--json",
                str(report_path),
            ],
            env={**os.environ, **env},
            # Not the repo: nothing in a run should resolve a path against the checkout.
            cwd=arm_dir,
            check=False,
        )
        try:
            return json.loads(report_path.read_text())[0]
        except OSError, json.JSONDecodeError, IndexError:
            return None

    return invoke


def build_arms(
    doc_label: str, doc: str, sections: list[str], agent_names: list[str]
) -> list[tuple[str, dict[str, str]]]:
    originals = {n: agent_source(n).read_text() for n in agent_names}

    def arm(doc_text: str | None) -> dict[str, str]:
        return {n: with_doc(md, doc_label, doc_text) for n, md in originals.items()}

    arms = [(BASELINE, arm(doc)), (WHOLE_DOC, arm(None))]
    arms += [(f"-{h}", arm(doc_without(doc, h))) for h in sections]
    return arms


def primary_root() -> Path:
    """The primary checkout, which a worktree's REPO is not."""
    common = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return Path(common).parent if common else REPO


def default_log() -> Path:
    """The primary checkout's instructions.log; a worktree has no log of its own."""
    return primary_root() / ".claude" / "logs" / "instructions.log"


def _print_summary(summary: dict, run: dict, spent: float) -> None:
    for arm, row in summary["measured"].items():
        print(f"{'CHANGED' if row['changed'] else 'same   '}  {arm}")
        for cid, r in row["cases"].items():
            mark = "*" if r["changed"] else " "
            mark = "?" if r["inconclusive"] else mark
            print(f"   {mark} {cid}: with {r['with']}, without {r['without']}")
    if summary["unmeasured"]:
        print(f"not measured: {', '.join(summary['unmeasured'])}")
    if run["stopped"]:
        print(f"stopped: {run['stopped']}")
    print(f"spent: ${spent:.2f}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser(
        "rank",
        help="rank repo docs by instructions.log loads, or one doc's sections by reads",
    )
    r.add_argument("--log", type=Path, default=None)
    r.add_argument("--top", type=int, default=10)
    r.add_argument(
        "--sections",
        metavar="DOC",
        default=None,
        help="rank this repo-relative doc's `## ` sections by transcripts naming them",
    )
    r.add_argument(
        "--transcripts",
        type=Path,
        action="append",
        default=[],
        help="a transcript directory (repeatable); default this repo's sessions",
    )
    a = sub.add_parser("run", help="ablate a doc's sections against the eval cases")
    a.add_argument("--doc", default="CLAUDE.md", help="repo-relative doc to ablate")
    a.add_argument(
        "--section",
        action="append",
        default=[],
        help="a `## ` heading to ablate (repeatable); default every section, in doc order",
    )
    a.add_argument("--case", action="append", default=[], help="case id (repeatable)")
    a.add_argument("--agent", action="append", default=[], help="agent (repeatable)")
    a.add_argument(
        "--by-reads",
        action="store_true",
        help="ablate sections most-read first, as `rank --sections` orders them",
    )
    a.add_argument(
        "--k",
        type=int,
        default=3,
        help="runs per case per arm; the case thresholds assume 3",
    )
    a.add_argument("--cap-usd", type=float, default=CAP_USD)
    a.add_argument("--out", type=Path, default=None)
    a.add_argument("--engine", type=Path, default=ENGINE)
    a.add_argument("--node", default="node")
    a.add_argument("--history", type=Path, default=HISTORY)
    a.add_argument(
        "--cost-reports",
        type=Path,
        action="append",
        default=[],
        help="a directory of past engine reports to price cases from (repeatable); "
        "default the ablation cache and the sweep archive",
    )
    a.add_argument(
        "--dry-run", action="store_true", help="print the plan, spend nothing"
    )
    args = p.parse_args(argv)

    def transcripts() -> list[Path]:
        dirs = getattr(args, "transcripts", None)
        if dirs:
            return sorted(p for d in dirs for p in d.rglob("*.jsonl"))
        return default_transcripts(primary_root())

    if args.cmd == "rank" and args.sections:
        headings = [h for h, _ in split_sections((REPO / args.sections).read_text())[1]]
        paths = transcripts()
        print(f"{args.sections}: sections by transcripts naming them, of {len(paths)}")
        for heading, n in section_reads(headings, paths):
            print(f"{n:6d}  ## {heading}")
        return EXIT_DONE

    if args.cmd == "rank":
        log = args.log or default_log()
        if not log.is_file():
            print(f"no instructions log at {log}; pass --log", file=sys.stderr)
            return EXIT_REFUSED
        # The rotated log too, so the ranking does not depend on when it last rotated.
        rotated = log.with_name(log.name + ".1")
        lines = log.read_text().splitlines()
        if rotated.is_file():
            lines += rotated.read_text().splitlines()
        for path, n in rank_docs(lines)[: args.top]:
            print(f"{n:6d}  {path}")
        return EXIT_DONE

    if args.cap_usd > CAP_USD:
        print(
            f"--cap-usd {args.cap_usd} exceeds the ${CAP_USD:.0f} ruling",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    doc = (REPO / args.doc).read_text()
    headings = [h for h, _ in split_sections(doc)[1]]
    sections = args.section or headings
    if args.by_reads:
        reads = dict(section_reads(sections, transcripts()))
        sections = sorted(sections, key=lambda h: -reads[h])
    unknown = [h for h in sections if h not in headings]
    if unknown:
        print(f"no such section in {args.doc}: {unknown}", file=sys.stderr)
        return EXIT_REFUSED
    cases = load_cases(args.case, args.agent)
    agent_names = sorted({c["agent"] for c in cases})
    clashes = [n for n in agent_names if (CHEZMOI_AGENTS / f"{n}.md").exists()]
    if clashes:
        print(
            f"the engine resolves these from chezmoi first: {clashes}", file=sys.stderr
        )
        return EXIT_REFUSED

    runs = (2 + len(sections)) * len(cases) * args.k
    print(
        f"{args.doc}: baseline, -doc and {len(sections)} section arm(s) x "
        f"{len(cases)} case(s) x k={args.k} = {runs} runs, capped at ${args.cap_usd:.2f}"
    )
    priced = run_costs(
        report_entries(args.cost_reports or [ABLATION_DIR, SWEEP_ARCHIVE])
    )
    per_run = {c["id"]: priced.get(c["id"], UNPRICED_RUN_USD) for c in cases}
    arm_usd = args.k * sum(per_run.values())
    finished = complete_arms(
        list(per_run.values()), args.k, 2 + len(sections), args.cap_usd
    )
    print(
        f"estimate: {sum(c in priced for c in per_run)} of {len(cases)} case(s) priced from "
        f"past reports, the rest at ${UNPRICED_RUN_USD:.2f} a run; ${arm_usd:.2f} an arm; "
        f"{max(finished - 2, 0)} of {len(sections)} section arm(s) expected to finish"
    )
    if finished < 3:
        print(
            f"refusing: the baseline, -doc and one section arm need "
            f"${3 * arm_usd:.2f}, and the cap leaves ${args.cap_usd - WORST_RUN_USD:.2f} "
            f"once a run's ${WORST_RUN_USD:.2f} worst case is held back",
            file=sys.stderr,
        )
        fit_cases, fit_sections = fitting_selection(
            list(per_run), per_run, args.k, sections, args.cap_usd
        )
        if fit_cases:
            flags = [f"--case {c}" for c in fit_cases]
            flags += [f"--section {shlex.quote(h)}" for h in fit_sections]
            print(f"largest selection that fits: {' '.join(flags)}", file=sys.stderr)
        else:
            print(
                "no selection fits: one case's three arms exceed the cap",
                file=sys.stderr,
            )
        return EXIT_REFUSED
    if args.dry_run:
        sizes = dict(split_sections(doc)[1])
        for h in sections:
            print(f"  {len(sizes[h]):6d} bytes  ## {h}")
        return EXIT_DONE

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is unset; a non-hermetic run is noise", file=sys.stderr
        )
        return EXIT_REFUSED
    history = json.loads(args.history.read_text()) if args.history.is_file() else {}

    def day_conflict() -> str | None:
        return sweep_day_conflict(
            datetime.now(timezone.utc).astimezone().date(), history
        )

    conflict = day_conflict()
    if conflict:
        print(f"refusing: {conflict}", file=sys.stderr)
        return EXIT_REFUSED

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Outside the checkout: an untracked file there parks the GitOps deployer.
    out = args.out or Path.home() / ".cache" / "homelab-evals" / "ablation" / stamp
    (out / "runs").mkdir(parents=True, exist_ok=True)
    arms = build_arms(args.doc, doc, sections, agent_names)
    budget = Budget(args.cap_usd)
    with tempfile.TemporaryDirectory(prefix="ablate-") as tmp:
        run = ablate(
            arms,
            cases,
            args.k,
            budget,
            engine_invoke(args.engine, args.node, out / "runs"),
            Path(tmp),
            # Re-checked per run, so a run started late on Saturday stops at midnight.
            refuse=day_conflict,
        )
    summary = summarize(run, [name for name, _ in arms], cases, args.k)
    report = {
        "ts": int(time.time()),
        "doc": args.doc,
        "cap_usd": args.cap_usd,
        "spent_usd": round(budget.spent, 4),
        "stopped": run["stopped"],
        **summary,
        "raw": run["results"],
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    _print_summary(summary, run, budget.spent)
    print(f"report: {out / 'report.json'}")
    return EXIT_CAPPED if run["stopped"] else EXIT_DONE


if __name__ == "__main__":
    raise SystemExit(main())
