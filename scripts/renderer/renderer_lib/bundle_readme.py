"""The bundle's root `README.md`: a static note on the layout `bundle.py` writes."""

README = "README.md"
# The bundle's layout, for a reader who pulls the artifact. It sits at the bundle root, which
# no Kustomization's `path` reaches, so Flux applies nothing from it. It counts for no name,
# so it lands under `_unclassified`, and a change to this text moves every Kustomization's
# changed set (C1, C2). Keep it free of anything per-revision.
BUNDLE_README = """# Homelab publish bundle

This artifact is what `scripts/renderer/homelab_render.py cluster` renders from the git tree.
Flux applies the parts below. Nothing here is hand-written.

- `flux/` holds one Kustomization file per published deploy tag and per shared kind, and
  `flux/kustomization.yaml`, which the root Kustomization `homelab` applies.
- `cluster/<name>/` holds the manifests that Kustomization `<name>` applies. A per-tag kind
  (`-deferred`, `-snapshot`, `-verify`) has its own directory beside its tag.
- `images/` holds the build contexts of the images the cluster builds, and `images/tags.json`.
- `digests.json` holds each name's `own` and `footprint` digest. `promote.py` reads it to find
  which Kustomizations a revision changes. A file outside the paths above, such as this one,
  is recorded under `_unclassified`, and a change there counts as every Kustomization changed.

Source of truth: scripts/renderer/renderer_lib/bundle.py in the homelab repository.
"""
