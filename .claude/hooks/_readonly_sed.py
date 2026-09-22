#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by auto-approve-readonly.py
"""The sed guard of the auto-approve-readonly Bash classifier.

`sed` reads by default and writes or executes only under -i, the w/W/r/R/e commands and the
s/// e/w flags, so the guard walks the script rather than the argv. Moved here byte-for-byte
from auto-approve-readonly.py when #2198 pushed that module over its length cap; the
verdict is unchanged. Imported by bare name from `auto-approve-readonly.py`; see
`_hook_common.py` for why that resolves both under the hook shim and under the tests.
Stdlib-only.
"""


def _sed_dangerous(script):
    """True if a sed script can write a file or execute a command.

    Walks the script skipping addresses and s///,y/// bodies so the command
    letters w/W/r/R/e (write-file, read-file, execute) and the s/// e/w flags
    are only matched in command position. Biased to reject: any parse ambiguity
    leaves more text to scan, which can only add rejections, never approvals.
    """
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c in " \t\n;{}!" or c.isdigit() or c in "$,~+-":
            i += 1  # separators / line addresses
            continue
        if c == "/":  # /regex/ address
            i += 1
            while i < n and script[i] != "/":
                i += 2 if script[i] == "\\" else 1
            i += 1
            continue
        if c == "\\" and i + 1 < n:  # \cregexc address (custom delim)
            delim = script[i + 1]
            i += 2
            while i < n and script[i] != delim:
                i += 2 if script[i] == "\\" else 1
            i += 1
            continue
        if c in ("s", "y"):  # s<d>..<d>..<d>flags / y<d>..<d>..<d>
            if i + 1 >= n:
                return True
            delim = script[i + 1]
            i += 2
            fields = 0
            while i < n and fields < 2:
                if script[i] == "\\":
                    i += 2
                    continue
                if script[i] == delim:
                    fields += 1
                i += 1
            flags = ""
            while i < n and script[i] not in " \t\n;}":
                flags += script[i]
                i += 1
            if c == "s" and ("e" in flags or "w" in flags):
                return True  # s///e executes, s///w writes
            continue
        if c in ("w", "W", "r", "R", "e"):
            return True  # write-file / read-file / execute
        i += 1  # p d n g h x b t : = l q c a i z ...
    return False


def _sed(argv):
    script, saw_script = [], False
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            i += 1
            if not saw_script and i < n:
                script.append(argv[i])
                saw_script = True
                i += 1
            break
        if a.startswith("-") and a != "-":
            if a.startswith("-i") or a.startswith("--in-place"):
                return None  # in-place edit writes
            if a == "-f" or a == "--file" or a.startswith("--file="):
                return None  # program file (uninspectable)
            if a in ("-e", "--expression"):
                if i + 1 >= n:
                    return None
                script.append(argv[i + 1])
                saw_script = True
                i += 2
                continue
            if a.startswith("-e"):
                script.append(a[2:])
                saw_script = True
                i += 1
                continue
            if a.startswith("--expression="):
                script.append(a.split("=", 1)[1])
                saw_script = True
                i += 1
                continue
            i += 1  # safe flags: -n -E -r -s -z ...
            continue
        if not saw_script:  # first positional is the script
            script.append(a)
            saw_script = True
        i += 1  # later positionals are input files
    if not saw_script or _sed_dangerous("\n".join(script)):
        return None
    return "sed"
