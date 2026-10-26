"""Map every closed depression (pothole) in each basin from the 4 m LiDAR DEM.

Method (Spatial Analyst):
  0. Remove road and rail embankments (NRN roads buffered 12 m; highways and OSM railways
     30 m; lowered to the nearby ground minimum) so crossings without visible culverts do not dam valleys
     into false depressions.
  1. Fill the DEM; depth = filled - DEM is the water depth each cell would hold
     before the depression spills.
  2. Keep cells deeper than MIN_DEPTH_M and group connected cells into depressions
     (Region Group, 8-connected).
  3. Zonal statistics give each depression's area, max depth and storage volume;
     depressions smaller than MIN_AREA_M2 are dropped, as are pits deeper than MAX_DEPTH_M
     (gravel and borrow pits, not wetlands).

Outputs:
  gdb/Basins                      basin boundaries with storage summary fields
  gdb/Depressions_<station>       one polygon per depression
  data/regions/<station>.tif      depression ID raster (used by 03_wetland_status.py)
  outputs/depression_summary.csv
"""
import sys
from pathlib import Path

import arcpy
import pandas as pd
from arcpy.sa import (Con, ExtractByAttributes, Fill, FocalStatistics, IsNull, NbrRectangle,
                      Raster, RegionGroup, SetNull, ZonalStatisticsAsTable)

from config import (BASINS, DEM_DIR, DEM_RES, GDB, MAX_DEPTH_M, MIN_AREA_M2, MIN_DEPTH_M,
                    OUTPUTS, RAW, ROOT, SR_WKID)

arcpy.env.overwriteOutput = True
arcpy.CheckOutExtension("Spatial")
REGIONS = ROOT / "data" / "regions"
CELL_M2 = DEM_RES ** 2
NRN_ROADS = str(RAW / "nrn" / "NRN_SK_15_0_GPKG_en.gpkg" / "main.NRN_SK_15_0_ROADSEG")
ROAD_BUFFER_M = 12  # half-width of a typical rural road embankment and ditch
# Rail grades and divided highways cross valleys on fills up to ~20 m high, so much wider at the base
WIDE_BUFFER_M = 30
MAJOR_ROADS = "ROADCLASS IN ('Freeway', 'Expressway / Highway', 'Arterial', 'Ramp')"


def load_basins():
    fc = str(GDB / "Basins")
    tmp = arcpy.conversion.JSONToFeatures(str(RAW / "basins.geojson"),
                                          f"{arcpy.env.scratchGDB}/basins", "POLYGON")
    arcpy.management.Project(tmp, fc, arcpy.SpatialReference(SR_WKID))
    ids = ",".join(f"'{s}'" for s in BASINS)
    lyr = arcpy.management.MakeFeatureLayer(fc, "drop", f"STATION_NUMBER NOT IN ({ids})")[0]
    arcpy.management.DeleteFeatures(lyr)  # basins removed from config since the download
    arcpy.management.AlterField(fc, "MAX_TOTAL_GROSS_SQKM_AREA", "GROSS_KM2", "Gross area (km²)")
    arcpy.management.AlterField(fc, "MAX_TOTAL_EFFECTIVE_SQKM_AREA", "EFFECTIVE_KM2",
                                "Effective area (km²)")
    arcpy.management.AddField(fc, "BASIN_NAME", "TEXT", field_length=60)
    with arcpy.da.UpdateCursor(fc, ["STATION_NUMBER", "BASIN_NAME"]) as cur:
        for st, _ in cur:
            cur.updateRow([st, BASINS[st]])
    return fc


def load_rails():
    """OpenStreetMap railways (active and abandoned) in the project CRS."""
    fc = str(GDB / "Rails")
    tmp = arcpy.conversion.JSONToFeatures(str(RAW / "rails.geojson"),
                                          f"{arcpy.env.scratchGDB}/rails", "POLYLINE")
    arcpy.management.Project(tmp, fc, arcpy.SpatialReference(SR_WKID))
    return fc


def lower_band(dem, lines, buffer_m, name):
    """Lower the cells within buffer_m of the lines to the lowest nearby cell outside the band."""
    buf = arcpy.analysis.PairwiseBuffer(lines, f"memory/{name}_buf", f"{buffer_m} Meters", "ALL")
    if int(arcpy.management.GetCount(buf)[0]) == 0:
        return dem
    arcpy.management.AddField(buf, "ON_BAND", "SHORT")
    arcpy.management.CalculateField(buf, "ON_BAND", "1")
    band_ras = f"{arcpy.env.scratchGDB}/{name}_ras"
    arcpy.conversion.PolygonToRaster(buf, "ON_BAND", band_ras, "CELL_CENTER", cellsize=dem)
    on_band = ~IsNull(Raster(band_ras))
    # Window reaches ~8 m past the band edge on both sides, wherever the cell sits in the band
    size = int(2 * buffer_m / DEM_RES) + 5 | 1
    ditch = FocalStatistics(SetNull(on_band, dem), NbrRectangle(size, size, "CELL"), "MINIMUM",
                            "DATA")
    return Con(on_band & ~IsNull(ditch), ditch, dem)


def remove_embankments(dem, station, rails_fc):
    """Cut road and rail embankments down to the lowest nearby ground (usually the ditch).

    LiDAR bare-earth DEMs do not see culverts, so every embankment acts as a dam and the
    valley upstream fills as one large false depression. Cutting the embankment down to
    ditch level mimics the culvert and restores the natural drainage path.
    """
    basin = arcpy.management.MakeFeatureLayer(str(GDB / "Basins"), f"b_{station}",
                                              f"STATION_NUMBER = '{station}'")[0]
    roads = arcpy.analysis.Clip(NRN_ROADS, basin, f"memory/roads_{station}")
    major = arcpy.management.MakeFeatureLayer(roads, f"major_{station}", MAJOR_ROADS)[0]
    rails = arcpy.analysis.Clip(rails_fc, basin, f"memory/rails_{station}")
    wide = arcpy.management.Merge([major, rails], f"memory/wide_{station}")
    dem = lower_band(dem, roads, ROAD_BUFFER_M, f"road_{station}")
    return lower_band(dem, wide, WIDE_BUFFER_M, f"wide_{station}")


def depressions(station, rails_fc):
    raw = Raster(str(DEM_DIR / f"{station}.tif"))
    # The NRN roads are geographic (NAD83); without this, rasterising them would carry
    # degrees into every downstream raster instead of the DEM's metre grid
    arcpy.env.outputCoordinateSystem = raw.spatialReference
    arcpy.env.snapRaster = raw
    arcpy.env.cellSize = raw
    arcpy.env.extent = raw.extent
    dem = remove_embankments(raw, station, rails_fc)
    depth = Fill(dem) - dem
    regions = RegionGroup(Con(depth >= MIN_DEPTH_M, 1), "EIGHT", "WITHIN", "NO_LINK")

    table = f"memory/zs_{station}"
    ZonalStatisticsAsTable(regions, "Value", depth, table, "DATA", "ALL")
    zs = pd.DataFrame(arcpy.da.TableToNumPyArray(table, ["Value", "COUNT", "MAX", "MEAN", "SUM"]))
    zs["area_m2"] = zs["COUNT"] * CELL_M2
    zs["volume_m3"] = zs["SUM"] * CELL_M2
    pits = zs.loc[zs["MAX"] > MAX_DEPTH_M, "Value"].tolist()
    zs = zs[(zs["area_m2"] >= MIN_AREA_M2) & (zs["MAX"] <= MAX_DEPTH_M)].rename(
        columns={"Value": "DEP_ID", "MAX": "max_depth_m", "MEAN": "mean_depth_m"})
    if pits:
        print(f"  excluded {len(pits)} pit(s) deeper than {MAX_DEPTH_M:g} m")

    # Keep only depressions above the size threshold (and not pits), then save the ID raster
    where = f"Count >= {int(MIN_AREA_M2 / CELL_M2)}"
    if pits:
        where += f" AND Value NOT IN ({','.join(str(int(p)) for p in pits)})"
    keep = ExtractByAttributes(regions, where)
    REGIONS.mkdir(parents=True, exist_ok=True)
    region_tif = REGIONS / f"{station}.tif"
    keep.save(str(region_tif))

    poly = str(GDB / f"Depressions_{station}")
    arcpy.conversion.RasterToPolygon(keep, poly, "NO_SIMPLIFY", "Value",
                                     "MULTIPLE_OUTER_PART")
    arcpy.management.AlterField(poly, "gridcode", "DEP_ID", "Depression ID")
    attrs = zs.set_index("DEP_ID")[["area_m2", "volume_m3", "max_depth_m", "mean_depth_m"]]
    for f in attrs.columns:
        arcpy.management.AddField(poly, f.upper(), "DOUBLE")
    with arcpy.da.UpdateCursor(poly, ["DEP_ID"] + [f.upper() for f in attrs.columns]) as cur:
        for row in cur:
            if row[0] in attrs.index:
                cur.updateRow([row[0]] + attrs.loc[row[0]].tolist())
            else:
                cur.deleteRow()
    zs["STATION_NUMBER"] = station
    return zs


def is_current(station):
    """True if this basin's outputs are newer than its DEM, the rails and the parameters."""
    region_tif = REGIONS / f"{station}.tif"
    if not region_tif.exists() or not arcpy.Exists(str(GDB / f"Depressions_{station}")):
        return False
    inputs = [DEM_DIR / f"{station}.tif", RAW / "rails.geojson", Path(__file__).with_name("config.py")]
    return region_tif.stat().st_mtime > max(p.stat().st_mtime for p in inputs)


def read_back(station):
    """Depression attributes from a previous run, in the same shape depressions() returns."""
    cols = ["DEP_ID", "AREA_M2", "VOLUME_M3", "MAX_DEPTH_M", "MEAN_DEPTH_M"]
    zs = pd.DataFrame(arcpy.da.TableToNumPyArray(str(GDB / f"Depressions_{station}"), cols))
    zs = zs.drop_duplicates("DEP_ID").rename(columns=str.lower).rename(columns={"dep_id": "DEP_ID"})
    zs["STATION_NUMBER"] = station
    return zs


def summarise(basins_fc, all_deps):
    area = {s: a for s, a in arcpy.da.SearchCursor(basins_fc, ["STATION_NUMBER", "SHAPE@AREA"])}
    rows = []
    for st, d in all_deps.groupby("STATION_NUMBER"):
        a = area[st]
        rows.append({
            "STATION_NUMBER": st, "basin": BASINS[st], "basin_km2": round(a / 1e6, 1),
            "n_depressions": len(d),
            "depressions_per_km2": round(len(d) / (a / 1e6), 1),
            "depression_area_pct": round(100 * d["area_m2"].sum() / a, 2),
            "storage_mm": round(1000 * d["volume_m3"].sum() / a, 1),
            "median_depression_ha": round(d["area_m2"].median() / 1e4, 2),
        })
    summary = pd.DataFrame(rows).sort_values("storage_mm")
    summary.to_csv(OUTPUTS / "depression_summary.csv", index=False)
    fields = ["n_depressions", "depression_area_pct", "storage_mm"]
    for f in fields:
        if f.upper() not in [x.name for x in arcpy.ListFields(basins_fc)]:
            arcpy.management.AddField(basins_fc, f.upper(), "DOUBLE")
    lookup = summary.set_index("STATION_NUMBER")
    with arcpy.da.UpdateCursor(basins_fc, ["STATION_NUMBER"] + [f.upper() for f in fields]) as cur:
        for row in cur:
            cur.updateRow([row[0]] + lookup.loc[row[0], fields].tolist())
    print(summary.to_string(index=False))


if __name__ == "__main__":
    OUTPUTS.mkdir(exist_ok=True)
    if not arcpy.Exists(str(GDB)):
        arcpy.management.CreateFileGDB(str(GDB.parent), GDB.name)
    basins_fc = load_basins()
    rails_fc = load_rails()  # before depressions() sets outputCoordinateSystem (breaks JSONToFeatures)
    # Basins finished by an earlier, interrupted run are reused; pass --force after changing
    # the method in this script so every basin is recomputed
    force = "--force" in sys.argv
    frames = []
    for st in BASINS:
        print(f"Depressions {st} ({BASINS[st]})")
        if not force and is_current(st):
            frames.append(read_back(st))
            print(f"  {len(frames[-1]):,} depressions >= {MIN_AREA_M2} m² (reused)")
            continue
        frames.append(depressions(st, rails_fc))
        print(f"  {len(frames[-1]):,} depressions >= {MIN_AREA_M2} m²")
    summarise(basins_fc, pd.concat(frames))
