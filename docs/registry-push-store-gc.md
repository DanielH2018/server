# Collecting the registry push store by hand

Use this page when monitor-bridge's `pvc_fullness` check pages on `registry-data`, the push
store's claim. Nothing collects that store on a schedule. Its weekly GC was retired in D5,
because the GC scaled `deploy/registry` to zero and Flux renders that Deployment's
`replicas: 1`. Flux's drift correction would restore the pod in the middle of the collection.
`ansible/roles/k8s/registry/CLAUDE.md`, *Notable*, has the growth rate and why the page is
the bound.

This procedure collects the store without that race. The operator suspends the Kustomization
that applies `deploy/registry` before the scale-down, so Flux writes nothing to
`spec.replicas` until the resume. The resume is what scales the registry back up.

Steps 1 and 2 are read-only, and an agent session may run them. Every later step is
**operator-only**. Step 3 deletes manifests from the store. Steps 4 to 7 need the k3s admin
kubeconfig (`sudo k3s kubectl`) or a write to the `DEPLOY_HOLD` repository variable, and an
agent session has neither.

## What frees bytes, and what does not

`garbage-collect` is the only thing that removes a blob from the store. It deletes the blobs
that no remaining manifest references, so deleting a tag or a manifest frees nothing on its own.
The registry must not serve while `garbage-collect` runs: a push during the sweep can lose a blob
that a new manifest references. Hence the order below. Delete the tags while the registry serves, then
stop it, collect, and start it again.

This procedure runs plain `garbage-collect`, without `--delete-untagged`. Step 3 deletes each
unwanted manifest explicitly, so the GC has nothing untagged to find. The flag would also delete
any manifest the store holds by digest alone, such as a signature stored as an OCI referrer, and
the store keeps the break-glass artifact and its signature (`docs/break-glass.md`).

## 1. See what fills the store

Run these on daniel-box. The push store answers on the loopback hostPort, and the node grant in
`ansible/roles/k8s/registry/templates/networkpolicy.yaml.j2` admits the request. From
daniel-server, the same reads answer on the pinned ClusterIP, `10.43.51.14:5000`.

```bash
uv run python scripts/diagnostics/probe.py metric \
  'kubelet_volume_stats_used_bytes{persistentvolumeclaim="registry-data"}'
curl -s http://localhost:5000/v2/_catalog
curl -s http://localhost:5000/v2/<repo>/tags/list
```

The verify Job's seed writes one `python-<12 hex>` tag in `registry-selftest` for each
`flux_job_image` pin, at about 20 MB each. Every other repository is named for an image
`k8s/image-builder` built before its role moved to GHCR, and nothing has pushed to one since.

On 2026-10-25 the claim held 3.98 GB. `registry-selftest` held one seed tag
(`python-f6a589d43c42`), `verify`, and `probe`, which the retired Ansible self-test wrote. Ten
legacy repositories held the rest: `code-server`, `homelab-mcp`, `ical-proxy`, `karakeep`,
`n8n`, `n8n-runners`, `nut`, `pi-peer-backup`, `terraria` and `valheim`. Each carried
`latest`, `buildcache` and up to three `sha-<12 hex>` content tags. Those legacy images, not
the seeds, are what a first collection reclaims.

## 2. Write down the keep set

Keep every manifest in this list. Delete nothing else in step 3 without a reason you can name.

- **The current seed.** The pull Jobs run it, so read its tag from the live Job rather than
  from the pin:

    ```bash
    kubectl -n homelab get jobs -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' | grep registry-selftest | sort -u
    ```

- **`registry-selftest:verify`.** The push check writes it on every run.
- **The whole `homelab-manifests` repository, if it exists.** Break-glass pushes its artifact
  and cosign signature there (`docs/break-glass.md`, *GitHub, GHCR or Sigstore is
  unreachable*). The store held no such repository on 2026-10-25.
- **Every repository a running workload still pulls from the push store.** The renderer
  rewrites a flux-owned role's `localhost:5000/<name>` to GHCR, so this list normally names
  only the current seed. On 2026-10-25 it printed `registry-selftest:python-f6a589d43c42`
  and nothing else. Prove it, rather than assume it:

    ```bash
    kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{range .spec.initContainers[*]}{.image}{"\n"}{end}{end}' | grep '^localhost:5000/' | sort -u
    ```

## 3. Delete what is not kept, while the registry serves

Deleting a manifest scales nothing, so this step needs no suspend. For each tag outside the keep
set, read its digest and delete the manifest by digest. The `Accept` header names every manifest
type the store holds, so the registry reports the digest of the stored manifest rather than a
converted one:

```bash
ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
repo=registry-selftest; tag=python-<12 hex>
digest="$(curl -sI -H "Accept: $ACCEPT" "http://localhost:5000/v2/$repo/manifests/$tag" | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}')"
echo "$repo:$tag -> $digest"
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "http://localhost:5000/v2/$repo/manifests/$digest"
```

A `202` is a deleted manifest. A delete by digest removes every tag that points at that digest,
so check that no kept tag shares it first. A legacy repository's `latest` and its newest
`sha-<12 hex>` tag share one digest, which is harmless when the whole repository goes.
Handing one of those roles back to Ansible rebuilds its image, because `k8s/image-builder`
skips a build only when the store serves both of its tags. The registry Deployment sets
`REGISTRY_STORAGE_DELETE_ENABLED`, which is what permits the delete.

If no tag is left to delete, stop here. A collection would free nothing, and the page needs a
larger claim or a fix to whatever step 1 found growing.

## 4. Hold promotions, then suspend the registry Kustomization

A hand suspend is not in `digests.json`. If `promote` publishes a revision that changes the
registry tag during the window, its `verify` waits on the suspended Kustomization until it times
out. Set the hold first, as `docs/break-glass.md` step 2 does, and note the reason you set:

```bash
gh variable set DEPLOY_HOLD --body <reason> -R DanielH2018/server
```

Then read which Kustomization applies the Deployment, and suspend it with the field manager
`homelab-operator`, so that Flux leaves the field alone (`docs/flux-deploy-plane.md`,
*Recovering Flux by hand*):

```bash
kubectl -n homelab get deploy registry -o jsonpath='{.metadata.labels.kustomize\.toolkit\.fluxcd\.io/name}{"\n"}'
sudo k3s kubectl -n flux-system patch kustomization registry --type merge \
  --field-manager=homelab-operator -p '{"spec":{"suspend":true}}'
```

The first command prints `registry` when the role's own Kustomization applies the Deployment.
Suspend whatever name it prints. Wait until
`kubectl -n flux-system get kustomization registry` reads `SUSPENDED True` before step 5.

## 5. Stop the registry and run the collection

```bash
sudo k3s kubectl -n homelab scale deploy/registry --replicas=0
sudo k3s kubectl -n homelab wait --for=delete pod -l app=registry --timeout=120s
IMAGE="$(kubectl -n homelab get deploy registry -o jsonpath='{.spec.template.spec.containers[0].image}')"
```

While the registry is down, no node can pull from `localhost:5000`, and break-glass has no
source to push to. No other workload notices, provided step 2's pod list was empty.

Apply this Job. Each field matches the retired `gc-job.yaml.j2`, which commit `766ec76b4`
deleted:

- **`nodeSelector`.** `registry-data` is ReadWriteOnce, so the Job can only attach on the
  registry's node.
- **uid and gid 1000.** The blob tree belongs to 1000:1000, and root without `DAC_OVERRIDE`
  cannot unlink another uid's files.
- **`backoffLimit: 0`.** A retry would restart the sweep on a store the first attempt was
  partway through changing.

```bash
sudo k3s kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: registry-gc-push-store
  namespace: homelab
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1200
  ttlSecondsAfterFinished: 604800
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: daniel-box
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      automountServiceAccountToken: false
      enableServiceLinks: false
      containers:
        - name: gc
          image: $IMAGE
          command: ["/bin/registry"]
          args: ["garbage-collect", "/etc/distribution/config.yml"]
          env:
            - name: REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY
              value: /var/lib/registry
            - name: REGISTRY_STORAGE_DELETE_ENABLED
              value: "true"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: ["ALL"]}
          volumeMounts:
            - name: registry-data
              mountPath: /var/lib/registry
      volumes:
        - name: registry-data
          persistentVolumeClaim:
            claimName: registry-data
EOF
sudo k3s kubectl -n homelab wait --for=condition=complete job/registry-gc-push-store --timeout=1200s
kubectl -n homelab logs job/registry-gc-push-store | tail -n 20
```

The command is `/bin/registry` itself, because the image's entrypoint starts a serving registry.
The log's last lines count the blobs eligible for deletion. If the Job fails, go to step 6
anyway. A stopped registry is the worse state, and the store is no less consistent than a
half-finished sweep left it.

## 6. Resume, then clear the hold

```bash
sudo k3s kubectl -n flux-system patch kustomization registry --type merge \
  --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'
```

Flux's next reconcile applies the rendered `replicas: 1`, which starts the registry again. Do not
scale it up by hand. A `kubectl scale` takes `spec.replicas` for the `kubectl` field manager, and
the resume is the step that hands it back to Flux. Wait until the Kustomization reads Ready and
`curl -s http://localhost:5000/v2/` answers `{}`.

Before you clear the hold, read it, and compare it with the reason you set in step 4:

```bash
gh variable get DEPLOY_HOLD -R DanielH2018/server
```

If it still reads your reason, clear it with
`gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`. If it names a SHA, a `promote`
run failed during the window. Work it by `docs/break-glass.md` step 4 instead of clearing it.

## 7. Check the result

Re-run step 1's usage query, and step 2's tag list for `registry-selftest`. The claim should read
lower by roughly the seeds you deleted. The next `registry-verify` run proves the registry
serves and that the kept seed still pulls. Delete the finished Job when you no longer need its
log:

```bash
sudo k3s kubectl -n homelab delete job registry-gc-push-store
```
