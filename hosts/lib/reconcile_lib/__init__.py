"""The helpers `hosts/lib/homelab-reconcile` imports: its activation step and its arms.

Only reconcile imports this package, so `hosts/lib/tests/_reconcile_size.py` counts it with
reconcile against the line ceiling. It uses the standard library only and runs under the hosts'
3.12 (`hosts/lib/tests/test_reconcile_component.py` parses it with that grammar).
"""
