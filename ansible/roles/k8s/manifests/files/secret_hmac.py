"""Keyed digest of one rendered secret manifest, for the release and render records (#2574).

Run by `tasks/release_digest.yml` on the deploy host, as root, through `uv run --python
{{ host_python_version }}` -- the pinned host interpreter, never the repo project.

  secret_hmac.py --ensure-key KEYFILE   create KEYFILE once (32 random bytes, 0600); print
                                        `created` or `present`
  secret_hmac.py KEYFILE FILE           print HMAC-SHA256(key, FILE bytes) as hex

WHY A HELPER RATHER THAN `openssl dgst -hmac`. That flag takes the key on argv, where it is
readable in `/proc/<pid>/cmdline` and lands in the registered task result's `cmd`. `no_log`
hides that result from output, but not from the host fact a later task reads. This process
receives only paths and prints only a digest, so neither the key nor the manifest's content
reaches an argv, a register or a log.

WHY KEYED. A plain sha256 of a manifest rendered from a known template lets anyone who reads a
0644 record test guesses at a low-entropy secret offline. With a host-local key, the digest is
comparable only between records written on the host that holds it.

Exit codes: 0 digest or key printed; 1 FILE unreadable (the caller records ABSENT); 2 KEYFILE
missing or unreadable (the caller omits the secret digest entirely).
"""

import hashlib
import hmac
import os
import sys

KEY_BYTES = 32


def ensure_key(path):
    """Create `path` with fresh random bytes unless it exists. Atomic under concurrent deploys.

    `O_EXCL` makes creation a single test-and-set in the kernel: of two deploys racing to
    create the key, exactly one writes it and the other reads the winner's bytes. A
    check-then-write would let the loser overwrite a key a record was already digested with.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return "present"
    with os.fdopen(fd, "wb") as fh:
        fh.write(os.urandom(KEY_BYTES))
    return "created"


def digest(key_path, file_path):
    """HMAC-SHA256 of `file_path`'s bytes under the key in `key_path`, as lowercase hex."""
    with open(key_path, "rb") as fh:
        key = fh.read()
    if len(key) < KEY_BYTES:
        raise KeyError(key_path)
    with open(file_path, "rb") as fh:
        return hmac.new(key, fh.read(), hashlib.sha256).hexdigest()


def main(argv):
    if len(argv) == 2 and argv[0] == "--ensure-key":
        print(ensure_key(argv[1]))
        return 0
    if len(argv) != 2:
        print(
            "usage: secret_hmac.py --ensure-key KEYFILE | KEYFILE FILE", file=sys.stderr
        )
        return 64
    key_path, file_path = argv
    if not os.access(key_path, os.R_OK):
        return 2
    try:
        print(digest(key_path, file_path))
    except KeyError:
        return 2
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
