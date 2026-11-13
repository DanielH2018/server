# ansible/

No playbook here applies anything. This tree holds the inventory, the k8s roles that the
renderer reads and Flux applies, the SOPS-encrypted secrets and the secret-rotation register.

The host bring-up runbook moved to
[`docs/host-bring-up.md`](../docs/host-bring-up.md). Its section numbers are unchanged, so a
citation of "`ansible/README.md` §9" now means §9 of that page.
