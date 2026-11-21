# run_tiles

Run `module_area_potentials` on a tiled version of a shape file. Tiling
keeps per-shape cutouts small so large regions can be processed in
parallel without any single job exploding in memory.

## Pipeline

1. `build_shape_tiles.py` clips a source shape parquet (for example
   `resources/inputs/shapes/europe.parquet`) into tiles under
   `resources/inputs/tiles/`. Each tile keeps the module's schema
   (`shape_id`, `country_id`, `shape_class`, `geometry`).
2. The Snakefile discovers every tile parquet, unzips any `*.gdb.zip`
   placed in `resources/inputs/wdpa/`, merges them into a single
   `wdpa_merged.gpkg`, and runs the module per tile.
3. The module splits each tile by `country_id` and processes countries
   in parallel. Outputs land under
   `resources/module/results/{tile}/base/`.

## Run it

From `run_tiles/`:

```shell
# 1. Build tiles. Example: 5 x 5 degree tiles around NLD + BEL + DEU.
pixi run python build_shape_tiles.py \
    resources/inputs/shapes/europe.parquet 0 45 15 55 \
    --resolution 5 --pre-buffer-maritime

# 2. Drop one or more WDPA .gdb.zip archives into resources/inputs/wdpa/
#    (download per-region from https://www.protectedplanet.net/).

# 3. Run the workflow.
pixi run snakemake --use-conda --cores 8
```

## Offshore buffer

Offshore wind siting excludes a coastal strip (for example 12 nautical
miles = 22 km for the Netherlands). If a tile holds a country's maritime
polygon but not its land, the runtime cannot compute the buffer. So
`--pre-buffer-maritime` bakes the buffer into the maritime geometry at
build time, using the full country land polygon and the distances in the
top-level `buffer_maritime` block of `config.yaml`:

```yaml
buffer_maritime:
  default: 10000   # 10 km
  overrides:
    NLD: 22000     # 22 km = 12 nautical miles
    PRT: 2000
```

To avoid double-buffering, `shapes_buffer.land` for `wind_offshore` is
kept at `0` at runtime. Baked tiles carry a `prebuffer_land_m` column
and a `module_area_potentials` pyarrow metadata key so they cannot be
mistaken for raw geoboundaries.

To change the buffer policy, edit `buffer_maritime` and rerun
`build_shape_tiles.py`.

## Adding WDPA regions or tiles

Drop new `*.gdb.zip` into `resources/inputs/wdpa/` or new tile parquets
into `resources/inputs/tiles/` and rerun snakemake. Already-computed
WDPA regions and tiles are not reprocessed.

## Scaling up to a global run

Two options when running large amount of tiles:

- `tiny_files: True` (default) streams slope and bathymetry from the
  Zenodo COGs per tile. Occasional HTTPS failures start to hurt at
  scale. Set `tiny_files: False` to download the global COGs once
  (~8 GB) and clip them locally; HTTP flakiness goes away.
- Add `--retries 3` to the snakemake command so any remaining transient
  failure (a WDPA unzip, a landcover re-download) is retried
  automatically.
