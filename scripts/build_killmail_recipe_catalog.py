#!/usr/bin/env python3
"""Pack already-extracted CCP blueprint snapshots; no downloads or database access."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

if __package__:
    from .killmail_recipe_history import DATA, canonical_json, raw_recipes
else:
    from killmail_recipe_history import DATA, canonical_json, raw_recipes


def pack(manifest_path, source_dir, output_dir):
    """Input manifest has version, snapshot_date and source for each snapshot.

    Each input is blueprints-<version>.json, an array of extracted CCP rows.
    Keep the input manifest in release order; never use mirror modification
    timestamps to reorder the dated legacy SDEs.
    """
    manifest = json.loads(manifest_path.read_text())
    snapshots = []
    public = []
    previous = {}
    for entry in manifest:
        raw = (source_dir / f"blueprints-{entry['version']}.json").read_bytes()
        recipes = raw_recipes(json.loads(raw))
        metadata = {key: entry[key] for key in ("version", "snapshot_date", "source")}
        date_kind = ("mirror_timestamp" if entry["version"] == "build-2960198" else
                     "ccp_release_timestamp" if entry["version"].startswith("build-") else
                     "sde_version_date")
        metadata.update(
            source_extracted_sha256=hashlib.sha256(raw).hexdigest(),
            recipes_sha256=hashlib.sha256(canonical_json(recipes)).hexdigest(),
            recipe_count=len(recipes), date_kind=entry.get("date_kind", date_kind),
        )
        for key in ("effective_from", "effective_source"):
            if key in entry:
                metadata[key] = entry[key]
        upsert = [[int(type_id), *recipes[type_id]] for type_id in sorted(recipes, key=int)
                  if recipes[type_id] != previous.get(type_id)]
        removed = sorted(int(type_id) for type_id in previous.keys() - recipes.keys())
        snapshots.append(dict(metadata, upsert=upsert, remove=removed))
        public.append(dict(metadata, changed_or_added=len(upsert), removed=len(removed)))
        previous = recipes
    output_dir.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(canonical_json({"format_version": 1, "snapshots": snapshots}), mtime=0)
    (output_dir / "killmail_recipe_history.json.gz").write_bytes(blob)
    (output_dir / "killmail_recipe_history_manifest.json").write_text(json.dumps({
        "format_version": 1, "catalog_sha256": hashlib.sha256(blob).hexdigest(),
        "snapshots": public,
    }, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DATA)
    args = parser.parse_args()
    pack(args.manifest, args.source_dir, args.output_dir)


if __name__ == "__main__":
    main()
