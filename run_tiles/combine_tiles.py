"""Resample per-tile TIFs to 0.25 deg and bundle four techs into one NetCDF.

For each tile under ``results/outputs/``, load the four per-technology
``area_potential_{tech}.tif`` files, downsample each to a 0.25 deg grid
using sum-aggregation (appropriate for area quantities in m^2), and write
them as four variables in a single NetCDF under
``results/quarter_degree/``.

The output grid is EPSG:4326 at 0.25 deg resolution (the ERA5 projection),
snapped to each tile's bbox so pixel edges align with tile boundaries. The
NetCDF uses ``latitude`` / ``longitude`` coordinate names to match ERA5.

Usage
-----
python combine_tiles.py [TILE_NAME ...]

With no arguments, processes every ``tile_*`` directory in
``results/outputs/``. With tile names, processes just those.
"""

import argparse
import datetime as dt
import math
import re
from pathlib import Path

import rasterio
import rioxarray as rxr
import xarray as xr
from rasterio.enums import Resampling

INPUT_DIR = Path("results/outputs")
OUTPUT_DIR = Path("results/quarter_degree")
TECHS = ["pv_rooftop", "pv_open_field", "wind_onshore", "wind_offshore"]
RESOLUTION_DEG = 0.25

# Matches directory names like `tile_0_0_20_20` or `tile_-180_-60_-160_-40`.
TILE_NAME_RE = re.compile(r"^tile_(-?\d+)_(-?\d+)_(-?\d+)_(-?\d+)$")


def _parse_tile_bounds(tile_name: str) -> tuple[int, int, int, int]:
    """Extract (xmin, ymin, xmax, ymax) from a tile directory name."""
    m = TILE_NAME_RE.match(tile_name)
    if not m:
        raise ValueError(f"Unrecognised tile name: {tile_name}")
    return tuple(int(v) for v in m.groups())  # type: ignore[return-value]


def _era5_target(
    xmin: float, ymin: float, xmax: float, ymax: float, res: float = RESOLUTION_DEG
) -> tuple[tuple[int, int], rasterio.Affine]:
    """Return (shape, transform) for a 0.25 deg grid snapped to the bbox.

    The bbox is snapped outward to whole multiples of ``res`` so pixel
    edges are at integer-multiple longitudes/latitudes.
    """
    xmin_s = math.floor(xmin / res) * res
    ymin_s = math.floor(ymin / res) * res
    xmax_s = math.ceil(xmax / res) * res
    ymax_s = math.ceil(ymax / res) * res
    nx = round((xmax_s - xmin_s) / res)
    ny = round((ymax_s - ymin_s) / res)
    transform = rasterio.transform.from_bounds(
        xmin_s, ymin_s, xmax_s, ymax_s, nx, ny
    )
    return (ny, nx), transform


def _resample_tech(
    tif_path: Path, shape: tuple[int, int], transform: rasterio.Affine
) -> xr.DataArray:
    """Resample a per-tile TIF onto the target grid using sum aggregation."""
    src = rxr.open_rasterio(tif_path, masked=True).squeeze("band", drop=True)
    return src.rio.reproject(
        src.rio.crs,
        shape=shape,
        transform=transform,
        resampling=Resampling.sum,
    )


def _output_path(tile_name: str) -> Path:
    """Where the NetCDF for this tile will be written."""
    xmin, ymin, xmax, ymax = _parse_tile_bounds(tile_name)
    return OUTPUT_DIR / f"area_potential_{xmin}_{ymin}_{xmax}_{ymax}.nc"


def convert_tile(tile_name: str) -> Path:
    """Convert one tile's four tech TIFs into a single NetCDF file."""
    xmin, ymin, xmax, ymax = _parse_tile_bounds(tile_name)
    shape, transform = _era5_target(xmin, ymin, xmax, ymax)

    data_vars: dict[str, xr.DataArray] = {}
    for tech in TECHS:
        tif = INPUT_DIR / tile_name / "base" / f"area_potential_{tech}.tif"
        if not tif.exists():
            raise FileNotFoundError(tif)
        da = _resample_tech(tif, shape, transform)
        da = da.rename({"x": "longitude", "y": "latitude"})
        da.attrs = {
            "long_name": f"{tech} area potential",
            "units": "m2",
        }
        data_vars[tech] = da

    ds = xr.Dataset(data_vars)
    ds.attrs = {
        "title": f"Area potential (0.25 deg) for {tile_name}",
        "resolution_deg": RESOLUTION_DEG,
        "projection": "EPSG:4326 (ERA5 grid)",
        "source": "module_area_potentials via run_tiles/combine_tiles.py",
        "tile_bbox": [xmin, ymin, xmax, ymax],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    ds["latitude"].attrs = {"standard_name": "latitude", "units": "degrees_north"}
    ds["longitude"].attrs = {"standard_name": "longitude", "units": "degrees_east"}

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"area_potential_{xmin}_{ymin}_{xmax}_{ymax}.nc"
    encoding = {t: {"zlib": True, "complevel": 4} for t in TECHS}
    ds.to_netcdf(out_path, encoding=encoding)
    print(f"Wrote {out_path}")
    return out_path


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "tile",
        nargs="*",
        help="Tile name(s) like tile_0_0_20_20. Empty = all tiles in "
        f"{INPUT_DIR}.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess tiles even when their NetCDF output already exists.",
    )
    args = parser.parse_args()

    if args.tile:
        tiles = list(args.tile)
    else:
        tiles = sorted(
            p.name
            for p in INPUT_DIR.iterdir()
            if p.is_dir() and TILE_NAME_RE.match(p.name)
        )
    if not tiles:
        raise SystemExit(f"No tiles found in {INPUT_DIR}.")

    # Skip tiles whose NetCDF already exists so reruns only process new or
    # missing tiles. Pass --force to overwrite everything.
    if not args.force:
        pending = [t for t in tiles if not _output_path(t).exists()]
        skipped = len(tiles) - len(pending)
        if skipped:
            print(f"Skipping {skipped} tile(s) with existing NetCDF output.")
        tiles = pending
    if not tiles:
        print("Nothing to do.")
        return

    for tile in tiles:
        try:
            convert_tile(tile)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"ERROR converting {tile}: {exc}")


if __name__ == "__main__":
    main()
