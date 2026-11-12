"""Where `homelab-secrets` writes a host's decrypted secrets, as the renderer and the census read it.

`renderer_lib.host_outputs` links each declared output's `path` to its rendered file under
`OUTPUT_ROOTS`, and `secrets_mgmt.secret_bearing_host_paths` lists those files for the hook
that refuses a read printing one. The hook's held copy cannot import the renderer, so the
roots live here, with no import of their own. `hosts/lib/homelab-secrets` keeps its own copy:
it ships to the host alone.
"""

# An output's `persist` flag picks its root.
OUTPUT_ROOTS = {False: "/run/homelab-secrets", True: "/var/lib/homelab-secrets"}
CREDSTORE = "/run/credstore"
