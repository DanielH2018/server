"""Puts the shared `_*.py` modules in reach of the subject directories under `hosts/tests/`.

pytest prepends this file's directory to `sys.path` when it loads it, so every group's tests
import the shared modules by bare name.
"""
