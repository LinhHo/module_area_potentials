"""Merge polygon layers from one or more WDPA File Geodatabases into one GeoPackage.

The area-potential module reads protected areas from a single file path (see
``workflow/scripts/clip_and_rasterise_polys.py``). WDPA is distributed per
region as ``*.gdb.zip`` archives, so when you want to run on areas spanning
multiple regions (e.g., Europe plus Asia), the regions have to be combined
into one file first.

Implementation note: regional WDPA polygon layers can contain hundreds of
thousands of multipolygons, which is too much to hold in Python memory at
once. We therefore call ``ogr2ogr`` from the installed GDAL, which streams
features from source to destination feature by feature. All attributes are
dropped (only the mask geometry is needed), everything is reprojected to
EPSG:4326, and polygons are promoted to multipolygons for a clean schema.

Usage
-----
python merge_wdpa.py OUTPUT.gpkg INPUT1.gdb [INPUT2.gdb ...]
"""

import argparse
import subprocess
from pathlib import Path

import pyogrio


TARGET_CRS = "EPSG:4326"
OUTPUT_LAYER = "protected_areas"


def polygon_layer_names(gdb_path: Path) -> list[str]:
    """Return the names of polygon layers in a File Geodatabase.

    ``pyogrio.list_layers`` returns an array of ``[name, geometry_type]``
    rows; we keep only the entries whose geometry type contains ``polygon``.
    """
    layers = pyogrio.list_layers(str(gdb_path))
    return [
        str(name)
        for name, geom_type in layers
        if geom_type is not None and "polygon" in str(geom_type).lower()
    ]


def append_layer(gdb_path: Path, layer: str, output_path: Path, append: bool) -> None:
    """Stream one source layer into the merged GeoPackage via ogr2ogr.

    ogr2ogr handles feature-by-feature streaming inside GDAL, so memory
    stays bounded even for very large regional WDPA layers.

    We pick geometry only via ``-sql "SELECT FID FROM <layer>"`` (geometry is
    an implicit OGR SQL column). ``-select ""`` would be the natural way to
    drop all attributes, but GDAL >= 3.11 rejects an empty field list,
    especially when combined with ``-update -append``.
    """
    cmd = [
        "ogr2ogr",
        "-f", "GPKG",
        "-t_srs", TARGET_CRS,
        "-nln", OUTPUT_LAYER,
        "-nlt", "PROMOTE_TO_MULTI",
        "-sql", f'SELECT FID FROM "{layer}"',
    ]
    if append:
        cmd += ["-update", "-append"]
    cmd += [str(output_path), str(gdb_path)]
    subprocess.run(cmd, check=True)


def merge(gdb_paths: list[Path], output_path: Path) -> None:
    """Merge polygon features from several .gdb files into one GeoPackage.

    Parameters
    ----------
    gdb_paths : list[Path]
        Paths to unzipped WDPA ``.gdb`` directories.
    output_path : Path
        Destination GeoPackage. Overwritten if it already exists.
    """
    if output_path.exists():
        output_path.unlink()

    wrote_any = False
    for gdb in gdb_paths:
        layer_names = polygon_layer_names(gdb)
        if not layer_names:
            print(f"Warning: no polygon layer in {gdb}; skipped.")
            continue
        for layer in layer_names:
            print(f"Streaming {gdb.name}:{layer} into {output_path.name}")
            append_layer(gdb, layer, output_path, append=wrote_any)
            wrote_any = True

    if not wrote_any:
        raise RuntimeError(
            "No polygon features found in any of the provided WDPA sources: "
            + ", ".join(str(p) for p in gdb_paths)
        )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output_path", type=Path, help="Output GeoPackage path.")
    parser.add_argument(
        "gdb_paths",
        type=Path,
        nargs="+",
        help="One or more unzipped WDPA .gdb directories.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    merge(args.gdb_paths, args.output_path)
