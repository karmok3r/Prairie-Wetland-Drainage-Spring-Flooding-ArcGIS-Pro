"""Classify every depression by how it has been used, 2009-2025 (AAFC Annual Crop Inventory).

For each year the 30 m crop inventory is resampled (nearest neighbour) onto the 4 m
depression grid, and each depression gets the share of its cells that are
  wet:     water (20), wetland (80) or "too wet to be seeded" (130)
  cropped: agriculture (120) and annual crop classes (131-199)
A year counts as wet when >= 25% of the depression is wet, and as cropped when >= 50% is.

Status:
  Persistent wetland      wet in >= 50% of years
  Intermittent wetland    wet in at least one year but < 50%
  Farmed through          never wet, cropped in >= 75% of years (consistent with drainage
                          or with a very short-lived wetland that is cultivated every year)
  Grassland / other       everything else

Outputs: STATUS, WET_YEARS, CROP_YEARS, N_YEARS fields on gdb/Depressions_<station>;
         outputs/wetland_status_summary.csv
"""
import arcpy
import numpy as np
import pandas as pd
from osgeo import gdal

from config import ACI_DIR, ACI_YEARS, BASINS, BENCHMARK, GDB, OUTPUTS, ROOT

gdal.UseExceptions()
arcpy.env.overwriteOutput = True
REGIONS = ROOT / "data" / "regions"
WET = [20, 80, 130]
STATUSES = ["Persistent wetland", "Intermittent wetland", "Farmed through", "Grassland / other"]
# Pomeroy et al. (2014): Smith Creek wetland area was 11% of the basin in 2008 (24% in 1958)
SMITH_CREEK_2008_PCT = 11.0


def status(wet, crop, n):
    if n == 0:
        return "Grassland / other"
    if wet >= 0.5 * n:
        return STATUSES[0]
    if wet > 0:
        return STATUSES[1]
    if crop >= 0.75 * n:
        return STATUSES[2]
    return STATUSES[3]


def classify(station):
    ds = gdal.Open(str(REGIONS / f"{station}.tif"))
    band = ds.GetRasterBand(1)
    ids = band.ReadAsArray().astype(np.int64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        ids[ids == nodata] = 0
    ids[ids < 0] = 0
    gt, (w, h) = ds.GetGeoTransform(), (ds.RasterXSize, ds.RasterYSize)
    bounds = (gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3])
    n_ids = ids.max() + 1
    cells = np.bincount(ids.ravel(), minlength=n_ids).astype(float)

    wet_years = np.zeros(n_ids)
    crop_years = np.zeros(n_ids)
    n_years = np.zeros(n_ids)
    for year in ACI_YEARS:
        tif = ACI_DIR / f"{station}_{year}.tif"
        if not tif.exists():
            continue
        aci = gdal.Warp("", str(tif), format="MEM", outputBounds=bounds, width=w, height=h,
                        resampleAlg="near").ReadAsArray()
        valid = aci > 0
        flat = ids.ravel()
        frac = lambda m: np.bincount(flat, weights=m.ravel(), minlength=n_ids) / np.maximum(cells, 1)
        covered = frac(valid) >= 0.5
        wet = frac(np.isin(aci, WET)) >= 0.25
        crop = frac((aci == 120) | ((aci >= 131) & (aci <= 199))) >= 0.5
        n_years += covered
        wet_years += wet & covered
        crop_years += crop & covered

    fc = str(GDB / f"Depressions_{station}")
    for name, ftype in [("WET_YEARS", "SHORT"), ("CROP_YEARS", "SHORT"), ("N_YEARS", "SHORT"),
                        ("STATUS", "TEXT")]:
        if name not in [f.name for f in arcpy.ListFields(fc)]:
            arcpy.management.AddField(fc, name, ftype, field_length=24 if ftype == "TEXT" else None)
    rows = []
    with arcpy.da.UpdateCursor(fc, ["DEP_ID", "AREA_M2", "VOLUME_M3", "WET_YEARS",
                                    "CROP_YEARS", "N_YEARS", "STATUS"]) as cur:
        for dep, area, vol, *_ in cur:
            s = status(wet_years[dep], crop_years[dep], n_years[dep])
            cur.updateRow([dep, area, vol, wet_years[dep], crop_years[dep], n_years[dep], s])
            rows.append((s, area, vol))
    return pd.DataFrame(rows, columns=["status", "area_m2", "volume_m3"])


def summarise(results):
    area = {s: a for s, a in arcpy.da.SearchCursor(str(GDB / "Basins"),
                                                    ["STATION_NUMBER", "SHAPE@AREA"])}
    rows = []
    for st, d in results.items():
        r = {"STATION_NUMBER": st, "basin": BASINS[st]}
        tot_a, tot_v = d["area_m2"].sum(), d["volume_m3"].sum()
        for s in STATUSES:
            sub = d[d["status"] == s]
            key = s.split()[0].lower().replace("/", "")
            r[f"{key}_n"] = len(sub)
            r[f"{key}_area_pct_of_depr"] = round(100 * sub["area_m2"].sum() / tot_a, 1)
            r[f"{key}_storage_pct"] = round(100 * sub["volume_m3"].sum() / tot_v, 1)
        wet_area = d.loc[d["status"].isin(STATUSES[:2]), "area_m2"].sum()
        r["wetland_area_pct_of_basin"] = round(100 * wet_area / area[st], 1)
        rows.append(r)
    out = pd.DataFrame(rows)
    out.to_csv(OUTPUTS / "wetland_status_summary.csv", index=False)
    print(out.to_string(index=False))
    smith = out.set_index("STATION_NUMBER").loc[BENCHMARK, "wetland_area_pct_of_basin"]
    print(f"\nSmith Creek benchmark: {smith}% of basin in wetland depressions (2009-2025) "
          f"vs {SMITH_CREEK_2008_PCT}% mapped for 2008 by Pomeroy et al. (2014)")


if __name__ == "__main__":
    results = {}
    for st in BASINS:
        print(f"Wetland status {st} ({BASINS[st]})")
        results[st] = classify(st)
        print("  " + results[st]["status"].value_counts().to_string().replace("\n", "\n  "))
    summarise(results)
