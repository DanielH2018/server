#!/usr/bin/env python3
"""Permission-audit report + segment-aware allowlist suggester.

Reads the per-host counts store written by .claude/hooks/log-permission.py and
reports: overall prompt rate, per-tool auto-approved vs. prompted counts, the
most-prompted commands, segment-aware suggested Bash allowlist rules (splits
compound `a && b | c` commands, ignores quoted/heredoc bodies and env prefixes,
cross-references the live allow/deny/ask tiers), a "left to prompt by design"
list of heads that are unsafe to blanket-allow, and a "redundant allow-rules" list of
existing allow entries that are provably dead (the auto-approve hook already covers
them, a broader rule subsumes them, or they're exact duplicates) and safe to prune.

Usage:
  uv run python .claude/scripts/audit-permissions.py            # full report
  uv run python .claude/scripts/audit-permissions.py --since 2026-06-01
  uv run python .claude/scripts/audit-permissions.py --json
  uv run python .claude/scripts/audit-permissions.py --prune 180        # prune the counts store
  uv run python .claude/scripts/audit-permissions.py --prune-dead       # remove dead allow-rules

Pure stdlib. Loads the hook module by path for the shared store/lock helpers.
"""
import importlib.util
import json
import os
import re
import sys
import time

_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "log-permission.py")
_spec = importlib.util.spec_from_file_location("log_permission", _HOOK)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)

# The auto-approve hook's read-only classifier, loaded by path (optional). Lets the
# audit flag concrete allow-rules the hook ALREADY covers — they never fire, so they're
# dead weight. If it can't load, hook-covered detection is simply skipped.
_AAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "auto-approve-readonly.py")
try:
    _aar_spec = importlib.util.spec_from_file_location("auto_approve_readonly", _AAR)
    aar = importlib.util.module_from_spec(_aar_spec)
    _aar_spec.loader.exec_module(aar)
except Exception:
    aar = None


def parse_args(argv):
    out = {"since": None, "prune": None, "prune_dead": False, "json": False, "error": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            out["json"] = True
        elif a == "--prune-dead":
            out["prune_dead"] = True
        elif a == "--since":
            if i + 1 >= len(argv):
                out["error"] = "--since requires a date value"
                break
            i += 1
            v = argv[i]
            try:
                lib.parse_ms(v)
            except Exception:
                out["error"] = "--since requires a valid date (got '" + v + "')"
                break
            out["since"] = v
        elif a == "--prune":
            if i + 1 >= len(argv):
                out["error"] = "--prune requires a number of days"
                break
            i += 1
            v = argv[i]
            try:
                n = int(v)
            except ValueError:
                n = None
            if n is None or n < 1:
                out["error"] = "--prune requires a positive integer of days (1 or more, got '" + v + "')"
                break
            out["prune"] = n
        else:
            out["error"] = "unknown argument: " + a
            break
        i += 1
    return out


def entries_array(store):
    return list(store["entries"].values())


def summarize_report(store, opts=None):
    opts = opts or {}
    rows = entries_array(store)
    if opts.get("since"):
        t = lib.parse_ms(opts["since"])
        rows = [e for e in rows if lib.parse_ms(e["last"]) >= t]
    by_tool = {}
    total_calls = total_asks = 0
    for e in rows:
        total_calls += e["calls"]
        total_asks += e["asks"]
        t = by_tool.get(e["tool"])
        if t is None:
            t = by_tool[e["tool"]] = {"tool": e["tool"], "calls": 0, "asks": 0, "auto": 0}
        t["calls"] += e["calls"]
        t["asks"] += e["asks"]
        t["auto"] += max(0, e["calls"] - e["asks"])
    prompted = sorted([e for e in rows if e["asks"] > 0], key=lambda e: e["asks"], reverse=True)
    return {"totalCalls": total_calls, "totalAsks": total_asks,
            "byTool": by_tool, "prompted": prompted, "list": rows}


# Multi-verb tools where the second token is a meaningful subcommand worth
# scoping the rule to (Bash(git push *)) rather than the whole tool (Bash(git *)).
SUBCOMMAND_TOOLS = {
    "git", "gh", "npm", "npx", "yarn", "pnpm", "cargo", "docker", "kubectl",
    "pip", "pip3", "go", "dotnet", "brew", "apt", "systemctl", "mullvad", "netsh",
    "uv", "ansible-playbook",
}

# Segment heads we must never propose blanket-allowing: arbitrary exec / deletion /
# network / opaque shells, plus `cat` (the canonical `cat > file` arbitrary-content
# write idiom — deliberately left prompting). A prompted command containing one of
# these is, by design, left to prompt each time — blocked_by_policy() surfaces it
# instead of suggest_rules().
UNSAFE_PREFIXES = {
    "cd", "rm", "rmdir", "mv", "dd", "cat", "sudo", "eval", "exec", "source", ".",
    "bash", "sh", "zsh", "pwsh", "powershell", "powershell.exe", "cmd", "cmd.exe",
    "find", "awk", "xargs", "chmod", "chown", "kill", "curl", "wget", "scp", "ssh",
}

# Segment heads that are meaningless to write a specific allow rule for: shell
# control words and pure-output builtins.
NOISE_PREFIXES = {
    "for", "if", "while", "until", "do", "done", "then", "else", "elif", "fi",
    "case", "esac", "function", "select", "time", "[", "[[", "{", "(", ":",
    "test", "echo", "printf", "true", "false",
}

_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_SUBCMD_TOKEN = re.compile(r"^[a-z][a-z0-9-]*$", re.IGNORECASE)
_BASH_RULE = re.compile(r"^Bash\((.+)\)$")


def split_segments(cmd):
    """Split a shell command into pipeline/sequence segments, but only on
    separators OUTSIDE quotes — so a `powershell.exe -Command "...|...|..."`
    one-liner stays a single segment instead of being shredded into fake
    cmdlet "segments". A heredoc body (<<EOF …) is inline data, not shell."""
    s = str(cmd or "")
    segs = []
    cur = ""
    q = None
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if q:
            cur += c
            if c == q:
                q = None
            i += 1
            continue
        if c == '"' or c == "'":
            q = c
            cur += c
            i += 1
            continue
        if c == "<" and i + 1 < n and s[i + 1] == "<":
            cur += s[i:]
            break
        if c == "\n" or c == ";":
            segs.append(cur)
            cur = ""
            i += 1
            continue
        if c == "&" and i + 1 < n and s[i + 1] == "&":
            segs.append(cur)
            cur = ""
            i += 2
            continue
        if c == "|" and i + 1 < n and s[i + 1] == "|":
            segs.append(cur)
            cur = ""
            i += 2
            continue
        if c == "|":
            segs.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    segs.append(cur)
    return [x.strip() for x in segs if x.strip()]


def candidate_prefix(segment):
    seg = re.sub(r"^[!({]\s*", "", str(segment or "").strip())
    tokens = [t for t in re.split(r"\s+", seg) if t]
    # Skip leading env-assignment prefixes (FOO=bar cmd …) to reach the real
    # command, but leave command-substitution assignments (T=$(…)) alone.
    while len(tokens) > 1 and _ENV_ASSIGN.match(tokens[0]) and not re.search(r"\$\(|`", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return None
    head = tokens[0]
    exact = len(tokens) == 1
    prefix = head
    if head in SUBCOMMAND_TOOLS and len(tokens) > 1 and _SUBCMD_TOKEN.match(tokens[1]):
        prefix = head + " " + tokens[1]
    return {"head": head, "prefix": prefix, "exact": exact}


def is_covered(prefix, patterns):
    """Does an existing allow/deny rule already cover commands of this prefix?"""
    for p in patterns or []:
        mm = _BASH_RULE.match(p)
        if not mm:
            continue
        pat = mm.group(1)
        if pat == "*":
            return True
        if re.search(r"\s\*$", pat):
            base = pat[:-2]
            if (prefix + " ").startswith(base + " "):
                return True
        elif prefix == pat:
            return True
    return False


def suggest_rules(prompted, opts=None):
    opts = opts or {}
    allow = opts.get("allow", [])
    deny = opts.get("deny", [])
    ask = opts.get("ask", [])
    groups = {}
    for e in prompted:
        if e["tool"] != "Bash":
            continue
        seen = set()
        for seg in split_segments(e["sum"]):
            cp = candidate_prefix(seg)
            if not cp:
                continue
            if cp["head"] in UNSAFE_PREFIXES or cp["head"] in NOISE_PREFIXES:
                continue
            # skip if already allowed (redundant) or deliberately set to prompt/block
            if is_covered(cp["prefix"], allow) or is_covered(cp["prefix"], deny) or is_covered(cp["prefix"], ask):
                continue
            rule = "Bash(" + cp["prefix"] + ")" if cp["exact"] else "Bash(" + cp["prefix"] + " *)"
            if rule in seen:  # credit a prefix at most once per command
                continue
            seen.add(rule)
            g = groups.get(rule)
            if g is None:
                g = groups[rule] = {"rule": rule, "asks": 0, "commands": 0, "examples": []}
            g["asks"] += e["asks"]
            g["commands"] += 1
            if len(g["examples"]) < 3:
                g["examples"].append(e["sum"])
    return sorted(groups.values(), key=lambda g: (g["asks"], g["commands"]), reverse=True)


def blocked_by_policy(prompted):
    """Prompted commands that will keep prompting because they contain a segment
    we refuse to blanket-allow (UNSAFE_PREFIXES). Explains the residual prompts."""
    groups = {}
    for e in prompted:
        if e["tool"] != "Bash":
            continue
        seen = set()
        for seg in split_segments(e["sum"]):
            cp = candidate_prefix(seg)
            if not cp or cp["head"] not in UNSAFE_PREFIXES or cp["head"] in seen:
                continue
            seen.add(cp["head"])
            g = groups.get(cp["head"])
            if g is None:
                g = groups[cp["head"]] = {"prefix": cp["head"], "asks": 0, "commands": 0}
            g["asks"] += e["asks"]
            g["commands"] += 1
    return sorted(groups.values(), key=lambda g: g["asks"], reverse=True)


def dead_allow_rules(perms):
    """Allow-rules that are provably inert — safe to remove. Three classes:

      - covered:   a concrete `Bash(cmd)` the auto-approve hook already classifies
                   read-only. The hook auto-approves before the allow-rule is ever
                   consulted, so the rule never fires.
      - subsumed:  a rule a BROADER allow rule already covers (e.g. `Bash(uv run *)`
                   subsumes `Bash(uv run pytest)`).
      - duplicate: the exact rule string appears more than once across the merged tiers
                   (e.g. listed in both settings.json and settings.local.json).

    Wildcard rules (`Bash(foo *)`, `…:*`) are patterns deliberately broader than the
    hook, so they are never reported as covered. The audit only SUGGESTS removal; it
    never edits settings — the operator reviews and prunes.
    """
    allow = (perms or {}).get("allow", []) or []
    dead = []
    seen = set()
    for rule in allow:
        if rule in seen:
            dead.append({"rule": rule, "reason": "duplicate (already listed)"})
            continue
        seen.add(rule)
        m = _BASH_RULE.match(rule)
        if not m:
            continue                                   # only Bash rules analyzed here
        inner = m.group(1)
        if inner.endswith("*") or ":*" in inner:
            continue                                   # a wildcard pattern, not concrete
        others = [r for r in allow if r != rule]
        cp = candidate_prefix(inner)
        if cp and is_covered(cp["prefix"], others):
            dead.append({"rule": rule, "reason": "subsumed by a broader allow rule"})
            continue
        if aar is not None:
            try:
                if aar.classify(inner) is not None:
                    dead.append({"rule": rule, "reason": "auto-approve hook already covers it"})
            except Exception:
                pass
    return dead


def compute_dead_local_prune(committed_allow, local_allow):
    """Decide which rules to remove from the machine-local allow list.

    `committed_allow` (settings.json) is treated as fixed context — never edited — so a
    local rule that merely duplicates a committed one is removed from local (the committed
    copy stays). Returns (new_local_allow, removed) where removed is [{rule, reason}].
    Pure function (no IO) for testability.
    """
    dead = dead_allow_rules({"allow": list(committed_allow) + list(local_allow)})
    queue = {}
    for d in dead:
        queue.setdefault(d["rule"], []).append(d["reason"])
    new_local, removed = [], []
    for rule in local_allow:
        reasons = queue.get(rule)
        if reasons:
            removed.append({"rule": rule, "reason": reasons.pop(0)})
            continue
        new_local.append(rule)
    return new_local, removed


def collect_permissions(objs):
    allow, deny, ask = [], [], []
    for o in objs or []:
        p = (o or {}).get("permissions")
        if not p:
            continue
        if isinstance(p.get("allow"), list):
            allow.extend(p["allow"])
        if isinstance(p.get("deny"), list):
            deny.extend(p["deny"])
        if isinstance(p.get("ask"), list):
            ask.extend(p["ask"])
    return {"allow": allow, "deny": deny, "ask": ask}


def load_perms():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    objs = []
    for f in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                objs.append(json.load(fh))
        except Exception:
            pass
    return collect_permissions(objs)


def do_prune(store, days, now_ms):
    cutoff = now_ms - days * 86400000
    removed = 0
    for k in list(store["entries"].keys()):
        if lib.parse_ms(store["entries"][k]["last"]) < cutoff:
            del store["entries"][k]
            removed += 1
    return removed


def do_prune_dead(base_dir):
    """Remove provably-dead allow-rules from settings.local.json (machine-local), backing
    it up first. settings.json (committed) is read-only context. Returns (removed, kept)."""
    committed = []
    try:
        with open(os.path.join(base_dir, "settings.json"), encoding="utf-8") as fh:
            committed = ((json.load(fh) or {}).get("permissions") or {}).get("allow") or []
    except Exception:
        pass
    local_path = os.path.join(base_dir, "settings.local.json")
    try:
        with open(local_path, encoding="utf-8") as fh:
            local_obj = json.load(fh)
    except Exception:
        return None, None                       # no local file -> nothing to do
    local_allow = ((local_obj.get("permissions") or {}).get("allow")) or []
    new_local, removed = compute_dead_local_prune(committed, local_allow)
    if not removed:
        return [], local_allow
    try:                                        # backup before writing
        with open(local_path + ".bak", "w", encoding="utf-8") as bf:
            json.dump(local_obj, bf, indent=2)
            bf.write("\n")
    except Exception:
        pass
    local_obj.setdefault("permissions", {})["allow"] = new_local
    with open(local_path, "w", encoding="utf-8") as fh:
        json.dump(local_obj, fh, indent=2)
        fh.write("\n")
    return removed, new_local


def pct(n, d):
    return round((n / d) * 100) if d else 0


def render(rep, perms=None):
    perms = perms or {"allow": [], "deny": []}
    lines = []
    lines.append("Permission audit — {} calls, {} prompts ({}% prompt rate)".format(
        rep["totalCalls"], rep["totalAsks"], pct(rep["totalAsks"], rep["totalCalls"])))
    lines.append("")
    lines.append("By tool:")
    for t in sorted(rep["byTool"].values(), key=lambda t: t["calls"], reverse=True):
        lines.append("  {}: {} calls, {} auto, {} prompted".format(
            t["tool"], t["calls"], t["auto"], t["asks"]))
    lines.append("")
    if rep["prompted"]:
        lines.append("Most-prompted commands (allowlist gaps):")
        for e in rep["prompted"][:20]:
            lines.append("  [{}x] {}: {}".format(e["asks"], e["tool"], e["sum"]))
        lines.append("")
        sugg = suggest_rules(rep["prompted"], perms)
        if sugg:
            lines.append("Suggested Bash allowlist rules (segment-aware, excludes already-allowed):")
            for s in sugg[:15]:
                lines.append("  {}   ({} prompts across {} cmds; e.g. {})".format(
                    s["rule"], s["asks"], s["commands"], s["examples"][0]))
            lines.append("")
        blocked = blocked_by_policy(rep["prompted"])
        if blocked:
            lines.append("Left to prompt by design (unsafe to blanket-allow):")
            for b in blocked[:10]:
                lines.append("  {}   ({} prompts across {} cmds)".format(
                    b["prefix"], b["asks"], b["commands"]))
    else:
        lines.append("No prompted commands recorded yet.")
    dead = dead_allow_rules(perms)
    if dead:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("Redundant allow-rules (safe to remove — {}):".format(len(dead)))
        for d in dead[:30]:
            lines.append("  {}   — {}".format(d["rule"], d["reason"]))
        if len(dead) > 30:
            lines.append("  … and {} more".format(len(dead) - 30))
    return "\n".join(lines)


def main():
    args = parse_args(sys.argv[1:])
    if args["error"]:
        sys.stderr.write("audit-permissions: " + args["error"] + "\n")
        sys.exit(1)
    if args["prune"] is not None:
        fd = lib.acquire_lock()
        if fd is None:
            sys.stderr.write("audit-permissions: could not acquire store lock; try again.\n")
            sys.exit(1)
        try:
            store = lib.load_store()
            if "entries" not in store:
                store["entries"] = {}
            removed = do_prune(store, args["prune"], int(time.time() * 1000))
            lib.save_store(store)
            print("Pruned {} entries not seen in {} days.".format(removed, args["prune"]))
        finally:
            lib.release_lock(fd)
        return
    if args["prune_dead"]:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        removed, kept = do_prune_dead(base)
        if removed is None:
            print("No settings.local.json to prune.")
        elif not removed:
            print("No redundant allow-rules to prune.")
        else:
            print("Pruned {} redundant allow-rule(s) from settings.local.json "
                  "(backup: settings.local.json.bak), kept {}:".format(len(removed), len(kept)))
            for r in removed:
                print("  - {}   ({})".format(r["rule"], r["reason"]))
        return
    store = lib.load_store()
    if "entries" not in store:
        store["entries"] = {}
    rep = summarize_report(store, {"since": args["since"]})
    perms = load_perms()
    if args["json"]:
        print(json.dumps({
            "totalCalls": rep["totalCalls"], "totalAsks": rep["totalAsks"],
            "byTool": rep["byTool"], "prompted": rep["prompted"],
            "suggestions": suggest_rules(rep["prompted"], perms),
            "blockedByPolicy": blocked_by_policy(rep["prompted"]),
            "deadAllowRules": dead_allow_rules(perms),
        }, indent=2))
    else:
        print(render(rep, perms))


if __name__ == "__main__":
    main()
