"""Offline, versioned CCP manufacturing recipes (raw quantities, not ME-adjusted)."""

import gzip
import hashlib
import json
from pathlib import Path


DATA = Path(__file__).with_name("data")
CATALOG = DATA / "killmail_recipe_history.json.gz"
MANIFEST = DATA / "killmail_recipe_history_manifest.json"


def canonical_json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


class RecipeHistory:
    """Resolve exact snapshots; never infer a valuation era from a loss date.

    Deltas retain material order, raw quantities and output yields. Loading a
    snapshot reconstructs the whole recipe set; current components are never
    silently mixed into a historical hull's manufacturing chain.
    """

    def __init__(self, catalog=CATALOG, manifest=MANIFEST):
        self.manifest = json.loads(Path(manifest).read_text())
        blob = Path(catalog).read_bytes()
        if self.manifest.get("format_version") != 1:
            raise ValueError("Unsupported recipe manifest format")
        if hashlib.sha256(blob).hexdigest() != self.manifest.get("catalog_sha256"):
            raise ValueError("Historical recipe catalog checksum mismatch")
        self.catalog = json.loads(gzip.decompress(blob))
        if self.catalog.get("format_version") != 1:
            raise ValueError("Unsupported recipe catalog format")
        self.versions = {row["version"]: row for row in self.manifest["snapshots"]}
        if len(self.versions) != len(self.manifest["snapshots"]):
            raise ValueError("Duplicate recipe versions")
        if [row["version"] for row in self.catalog["snapshots"]] != list(self.versions):
            raise ValueError("Recipe manifest and catalog versions differ")

    def resolve(self, version):
        # ISO dates are exact aliases for dated legacy SDEs, not an as-of
        # search through sparse snapshots. Native SDEs use their build ID.
        if len(version) == 10 and version[4] == "-" and version[7] == "-":
            version = version.replace("-", "")
        if version not in self.versions:
            raise ValueError(
                f"Unknown historical recipe version {version!r}; "
                "use --list-recipe-versions to list exact snapshots"
            )
        return version

    def recipes(self, version):
        version = self.resolve(version)
        recipes = {}
        for snapshot in self.catalog["snapshots"]:
            for type_id in snapshot["remove"]:
                recipes.pop(str(type_id), None)
            for type_id, output, materials in snapshot["upsert"]:
                recipes[str(type_id)] = [output, materials]
            if snapshot["version"] == version:
                expected = self.versions[version]
                if (len(recipes) != expected["recipe_count"] or
                        hashlib.sha256(canonical_json(recipes)).hexdigest() != expected["recipes_sha256"]):
                    raise ValueError(f"Historical recipes checksum mismatch: {version}")
                return recipes
        raise ValueError(f"Historical recipe version missing from catalog: {version}")

    def blueprint_rows(self, version):
        for type_id, (output, materials) in self.recipes(version).items():
            yield {"activities": {"manufacturing": {
                "products": [{"typeID": int(type_id), "quantity": output}],
                "materials": [{"typeID": material, "quantity": quantity}
                              for material, quantity in materials],
            }}}
