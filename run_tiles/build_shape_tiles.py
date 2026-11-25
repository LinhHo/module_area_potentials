"""Split a shape parquet into per-tile parquets for module_area_potentials.

Keeps the module's schema (shape_id, country_id, shape_class, geometry) and
clips each polygon to the tile bbox. With ``--pre-buffer-maritime`` the
script also subtracts each country's land buffer from its maritime polygons
BEFORE tiling, using the distances in the ``buffer_maritime`` top-level
section of ``config.yaml``:

    buffer_maritime:
      default: 10000
      overrides:
        NLD: 22000
        PRT: 2000

The buffer is computed on the full country polygon so tile boundaries never
truncate it. Set ``shapes_buffer.land: 0`` for wind_offshore at runtime to
avoid double-buffering (the other techs do not use maritime shapes, so no
other buffer settings need to change).

Baked tiles carry a ``prebuffer_land_m`` column and file-level metadata
(pyarrow schema key ``module_area_potentials``) so they are not mistaken
for raw geoboundaries.

Usage
-----
python build_shape_tiles.py SOURCE.parquet MINX MINY MAXX MAXY \
    [--resolution 5] [--pre-buffer-maritime]

Example around NLD + BEL + DEU
------------------------------
python build_shape_tiles.py \
    resources/inputs/shapes/europe.parquet 0 45 15 55 \
    --resolution 5 --pre-buffer-maritime
"""

import argparse
import datetime as dt
import io
import json
import math
from pathlib import Path

import geopandas as gpd
import pyarrow.parquet as pq
import shapely
import yaml
from shapely.geometry import box

SHAPE_COLUMNS = ["shape_id", "country_id", "shape_class", "geometry"]
# Optional columns passed through if present in the source; the module's
# report.py expects parent_name.
OPTIONAL_COLUMNS = ["parent_name"]

# Countries dropped before tiling. GlobCover 2009 has no land-cover data
# south of ~-65 deg S, so any tile containing ATA land fails resample.py
# with NoDataInBounds. Solar / wind area potential on polar ice is also
# not meaningful, so excluding Antarctica entirely is the honest fix.
EXCLUDED_COUNTRIES = {"ATA"}
METADATA_KEY = b"module_area_potentials"
CONFIG_PATH = Path("config.yaml")  # relative to CWD (run_tiles/)

# Intersection/difference against tile edges can leave 1-D fragments (lines,
# points) inside a GeometryCollection when the source polygon's boundary only
# grazes the tile edge (e.g. a tripoint). These collapse bounding boxes and
# crash the module's clip_box, so we keep only polygonal parts and drop
# anything whose area rounds to zero.
MIN_AREA_DEG2 = 1e-12


def _polygonal(geom):
    """Return the polygonal part(s) of a geometry (Polygon/MultiPolygon or empty)."""
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        parts = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        return shapely.unary_union(parts) if parts else shapely.Polygon()
    return shapely.Polygon()


def _drop_degenerate(gdf):
    """Remove rows that are empty or whose area rounds to zero."""
    return gdf[~gdf.geometry.is_empty & (gdf.geometry.area > MIN_AREA_DEG2)]


def _tile_bboxes(minx, miny, maxx, maxy, size):
    """Yield (xmin, ymin, xmax, ymax) tiles covering the domain."""
    lon = minx
    while lon < maxx:
        lat = miny
        while lat < maxy:
            yield lon, lat, min(lon + size, maxx), min(lat + size, maxy)
            lat += size
        lon += size


def _read_buffer_config():
    """Return (buffer_crs, default_m, {country_id: m}) from config.yaml.

    Reads the dedicated top-level ``buffer_maritime`` section (not the
    runtime ``shapes_buffer`` block, which stays at 0 to prevent
    double-buffering). The projection used for the buffer comes from the
    module's own ``buffer_crs`` key so the pre-bake matches what the runtime
    would have applied.
    """
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    if "buffer_maritime" not in cfg:
        raise KeyError(
            f"`buffer_maritime` section missing from {CONFIG_PATH}. "
            "Add it with `default` and `overrides` keys to use "
            "--pre-buffer-maritime."
        )
    bm = cfg["buffer_maritime"]
    buffer_crs = str(cfg["module_area_potentials"]["buffer_crs"])
    default_m = float(bm.get("default", 0))
    overrides = {str(k): float(v) for k, v in bm.get("overrides", {}).items()}
    return buffer_crs, default_m, overrides


def _prebuffer_maritime(shapes, buffer_crs, default_m, overrides_m):
    """Subtract country-level land buffer from each country's maritime polygons.

    Only countries with maritime shapes are processed, and each country's land
    is projected and buffered individually to keep peak memory low.
    """
    result = shapes.copy()
    result["prebuffer_land_m"] = 0.0
    maritime_countries = set(
        result.loc[result["shape_class"] == "maritime", "country_id"].unique()
    )
    for country_id in maritime_countries:
        distance_m = overrides_m.get(country_id, default_m)
        if distance_m <= 0:
            continue
        land_rows = result[
            (result["country_id"] == country_id) & (result["shape_class"] == "land")
        ]
        if land_rows.empty:
            print(f"  {country_id}: no land; skipped.")
            continue
        land_union = shapely.unary_union(land_rows.geometry.values)
        land_buffer = (
            gpd.GeoSeries([land_union], crs=shapes.crs)
            .to_crs(buffer_crs)
            .buffer(distance_m)
            .to_crs(shapes.crs)
            .iloc[0]
        )
        # Re-projecting back to EPSG:4326 can introduce tiny self-intersections
        # near the antimeridian; make_valid cleans them up.
        land_buffer = shapely.make_valid(land_buffer)
        mask = (result["country_id"] == country_id) & (
            result["shape_class"] == "maritime"
        )
        # Countries whose land or EEZ cross the 180th meridian (Fiji,
        # Kiribati, Russia Far East, …) can still produce TopologyException in
        # the difference. Catch it, skip the buffer for that country with a
        # clear message, and keep the raw maritime polygon rather than killing
        # the whole run.
        try:
            trimmed = result.loc[mask, "geometry"].difference(land_buffer)
            trimmed = [_polygonal(shapely.make_valid(g)) for g in trimmed.values]
        except shapely.errors.GEOSException as exc:
            print(
                f"  {country_id}: pre-buffer failed ({exc}); "
                "keeping raw maritime polygon (likely antimeridian crossing)."
            )
            continue
        result.loc[mask, "geometry"] = trimmed
        result.loc[mask, "prebuffer_land_m"] = distance_m
        print(f"  {country_id}: buffered maritime by {distance_m:g} m.")
    return _drop_degenerate(result).reset_index(drop=True)


def _write_tile(gdf, output_path, metadata):
    """Write a GeoParquet with custom file-level metadata attached."""
    buf = io.BytesIO()
    gdf.to_parquet(buf)
    buf.seek(0)
    table = pq.read_table(buf)
    meta = dict(table.schema.metadata or {})
    meta[METADATA_KEY] = json.dumps(metadata).encode("utf-8")
    pq.write_table(table.replace_schema_metadata(meta), output_path)


def build_tiles(source, output_dir, minx, miny, maxx, maxy, size, pre_buffer):
    """Write one tile parquet per non-empty tile bbox."""
    gdf = gpd.read_parquet(source)
    missing = [c for c in SHAPE_COLUMNS if c not in gdf.columns]
    if missing:
        raise ValueError(f"Source {source} missing column(s): {missing}.")
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        raise ValueError(f"Source CRS must be EPSG:4326 (got {gdf.crs}).")
    # Drop countries we cannot process (see EXCLUDED_COUNTRIES comment).
    dropped = gdf["country_id"].isin(EXCLUDED_COUNTRIES)
    if dropped.any():
        print(
            f"Excluding {dropped.sum()} rows for: {sorted(EXCLUDED_COUNTRIES)}"
        )
        gdf = gdf[~dropped]
    keep_cols = SHAPE_COLUMNS + [c for c in OPTIONAL_COLUMNS if c in gdf.columns]
    shapes = gdf[keep_cols].copy()
    for c in OPTIONAL_COLUMNS:
        if c not in shapes.columns:
            shapes[c] = None

    if pre_buffer:
        buffer_crs, default_m, overrides_m = _read_buffer_config()
        print(
            f"Pre-buffering maritime in {buffer_crs}: "
            f"default {default_m} m, overrides {overrides_m}"
        )
        # Clip to an enlarged domain bbox before buffering so huge geometries
        # (e.g. FRA with overseas territories) do not blow up memory.
        margin_deg = max(default_m, *overrides_m.values(), 0) / 111_000 + 0.5
        domain = box(
            minx - margin_deg, miny - margin_deg,
            maxx + margin_deg, maxy + margin_deg,
        )
        shapes = shapes[shapes.intersects(domain)].copy()
        shapes["geometry"] = [
            _polygonal(shapely.make_valid(g))
            for g in shapes.geometry.intersection(domain).values
        ]
        shapes = _drop_degenerate(shapes).reset_index(drop=True)
        shapes = _prebuffer_maritime(shapes, buffer_crs, default_m, overrides_m)
        prebuffer_info = {
            "buffer_crs": buffer_crs,
            "default_m": default_m,
            "overrides_m": overrides_m,
            "note": (
                "Maritime polygons already have the per-country land buffer "
                "subtracted. Keep shapes_buffer.land: 0 for wind_offshore at "
                "runtime to avoid double-buffering."
            ),
        }
    else:
        shapes["prebuffer_land_m"] = 0.0
        prebuffer_info = None

    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for xmin, ymin, xmax, ymax in _tile_bboxes(minx, miny, maxx, maxy, size):
        tile = box(xmin, ymin, xmax, ymax)
        candidates = shapes[shapes.intersects(tile)]
        if candidates.empty:
            continue
        clipped = candidates.copy()
        clipped["geometry"] = [
            _polygonal(shapely.make_valid(g))
            for g in candidates.geometry.intersection(tile).values
        ]
        clipped = _drop_degenerate(clipped).reset_index(drop=True)
        if clipped.empty:
            continue
        path = output_dir / f"tile_{xmin:g}_{ymin:g}_{xmax:g}_{ymax:g}.parquet"
        metadata = {
            "source": str(source),
            "tile_bbox": [xmin, ymin, xmax, ymax],
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "prebuffer_maritime": prebuffer_info,
        }
        _write_tile(clipped, path, metadata)
        written.append(path)
        n_land = int((clipped["shape_class"] == "land").sum())
        n_mar = int((clipped["shape_class"] == "maritime").sum())
        print(f"Wrote {path.name} ({n_land} land, {n_mar} maritime)")
    return written


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Split a shape parquet into per-tile parquets."
    )
    parser.add_argument("source", type=Path, help="Source shape GeoParquet.")
    parser.add_argument("minx", type=float)
    parser.add_argument("miny", type=float)
    parser.add_argument("maxx", type=float)
    parser.add_argument("maxy", type=float)
    parser.add_argument("--resolution", type=float, default=5)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("resources/inputs/tiles")
    )
    parser.add_argument(
        "--pre-buffer-maritime",
        action="store_true",
        help="Bake per-country land buffer into maritime polygons using "
        "distances from config.yaml.",
    )
    args = parser.parse_args()

    if not all(math.isfinite(v) for v in (args.minx, args.miny, args.maxx, args.maxy)):
        raise SystemExit("Error: bounding coordinates must be finite.")
    if not (-180 <= args.minx < args.maxx <= 180):
        raise SystemExit("Error: longitude must satisfy -180 <= minx < maxx <= 180.")
    if not (-90 <= args.miny < args.maxy <= 90):
        raise SystemExit("Error: latitude must satisfy -90 <= miny < maxy <= 90.")
    if not math.isfinite(args.resolution) or args.resolution <= 0:
        raise SystemExit("Error: resolution must be positive.")
    if args.pre_buffer_maritime and not CONFIG_PATH.exists():
        raise SystemExit(f"Error: {CONFIG_PATH} not found; run from run_tiles/.")

    written = build_tiles(
        source=args.source,
        output_dir=args.output_dir,
        minx=args.minx,
        miny=args.miny,
        maxx=args.maxx,
        maxy=args.maxy,
        size=args.resolution,
        pre_buffer=args.pre_buffer_maritime,
    )
    print(f"\nWrote {len(written)} tile(s) to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
