"""What each workflow job downloads from: a release repo, another host, or neither.

`release_fetches` splits each step's run into its curl and wget invocations and its
`gh release download` calls. An invocation fed by a pipe is a registry or API call, and a
probe throws its body away; neither counts. Each other one reads as the release repo, or
failing that the host, of the URL it names, or of the URL its variables hold.

An invocation is a probe when every file it writes with `-o`, `--output` or `-O` is
/dev/null, or, when it names no such file, when it redirects its stdout there (`>`, `1>` or
`&>`). Its own words end at the first `;`, `&&`, `||` or `|`, so a later command's redirect
does not count. A redirect of stderr alone (`2>/dev/null`) never makes a probe, because the body
still lands in the file or on stdout.
"""

import re

# A path segment; a `${{ … }}` expression counts as one, spaces and all.
SEGMENT = r"(?:\$\{\{[^}]*\}\}|[^\s\"'/])+"
RELEASE = re.compile(
    rf"github\.com/({SEGMENT}(?:/{SEGMENT})?)/releases/(?:latest/)?download\b"
)
# The repo a fetch whose text names no literal repo reads as; no EXEMPT row can name it.
UNRESOLVED = "<no literal repo>"
GH_RELEASE = re.compile(r"\bgh release download\b[^\n]*")
# A literal repo after -R or --repo; an invocation without one reads as UNRESOLVED.
GH_REPO = re.compile(r"(?:-R|--repo)[ =][\"']?([\w.-]+/[\w.-]+)[\"']?(?=[\s;&|)]|$)")
# Where a curl or wget invocation starts; group 1 is what precedes it on its line.
INVOCATION = re.compile(r"(^|.*?[\s;&|(`])(?:curl|wget)\s", re.M)
URL = re.compile(r"https?://(?:\$\{\{[^}]*\}\}|[^\s\"'])+")
HOST = re.compile(r"https?://([^/\s\"']+)")
VARIABLE = re.compile(r"\$(?:\{(\w+)[^}]*\}|(\w+))")
ASSIGNMENT = re.compile(r"^\s*(?:local\s+)?(\w+)=\"?([^\"\n]*)", re.M)
# The file an -o, --output or wget -O option names; curl's -O names the URL here, never /dev/null.
OUTPUT = re.compile(r"(?<=\s)(?:-[A-Za-z]*[oO]|--output)[\s=]*(\S+)")
# A redirect of stdout; the lookbehind leaves `2>/dev/null` out.
STDOUT_TO_NULL = re.compile(r"(?<!\d)(?:&|1)?>\s*/dev/null(?=[\s;&|)]|$)")
# Where an invocation's own words end and the next command's begin.
OWN_WORDS_END = re.compile(r"\s*(?:;|&&|\|\|?)")


def _is_probe(text: str) -> bool:
    """Whether a curl or wget invocation throws its body away."""
    own = OWN_WORDS_END.split(text, 1)[0]
    if files := [f.strip("\"'") for f in OUTPUT.findall(own)]:
        return all(f == "/dev/null" for f in files)
    return bool(STDOUT_TO_NULL.search(own))


def _downloads(run: str) -> list[str]:
    """The text of each curl or wget invocation in `run` that downloads.

    An invocation runs to the next one or the end of its line, continuations joined. One fed
    by a pipe is a registry or API call, and a probe (see the module docstring) throws its
    body away.
    """
    found = []
    for line in run.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        starts = list(INVOCATION.finditer(line))
        ends = [m.end(1) for m in starts[1:]] + [len(line)]
        for m, end in zip(starts, ends, strict=False):
            text = line[m.end(1) : end]
            piped = m[1].rstrip().endswith("|") and not m[1].rstrip().endswith("||")
            if not piped and not _is_probe(text):
                found.append(text)
    return found


def _source(url: str) -> str:
    """The release repo a URL downloads from, or its host when it is no release URL."""
    if m := RELEASE.search(url):
        source = m[1]
    else:
        source = m[1] if (m := HOST.search(url)) else UNRESOLVED
    return UNRESOLVED if "$" in source or "{" in source else source


def _fetched_repos(step: dict, env: dict) -> set[str]:
    """The release repos, or other hosts, a step's curl, wget or `gh release download` reads.

    An invocation naming no literal URL reads the URL its variables hold, from the step's own
    assignments or the step, job and workflow env.
    """
    run = str(step.get("run", ""))
    values = {**env, **(step.get("env") or {}), **dict(ASSIGNMENT.findall(run))}
    repos = {
        m[1] if (m := GH_REPO.search(c)) else UNRESOLVED
        for c in GH_RELEASE.findall(run)
    }
    for text in _downloads(run):
        for _ in range(3):
            if URL.search(text):
                break
            text = VARIABLE.sub(lambda v: str(values.get(v[1] or v[2], v[0])), text)
        repos |= {_source(url) for url in URL.findall(text)} or {UNRESOLVED}
    return repos


def release_fetches(sources: dict[str, dict]) -> set[tuple[str, str, str]]:
    """Every (file, job, source) a step outside pinned-tool downloads from inline."""
    return {
        (name, job_name, repo)
        for name, workflow in sources.items()
        for job_name, job in (workflow.get("jobs") or {}).items()
        for step in job.get("steps") or []
        for repo in _fetched_repos(
            step, {**(workflow.get("env") or {}), **(job.get("env") or {})}
        )
    }
