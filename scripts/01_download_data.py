"""Download all open data used in the project.

Sources (Open Government Licence - Canada unless noted):
  * AAFC Watersheds Project 2013: gross and effective drainage areas per gauge
  * Water Survey of Canada daily discharge (HYDAT) via the MSC GeoMet OGC API
  * NRCan HRDEM 2 m LiDAR mosaic (cloud-optimized GeoTIFF), read at its 4 m overview
  * AAFC Annual Crop Inventory 2009-2025 (30 m), via the AAFC ImageServer
  * National Road Network (NRN) for Saskatchewan, Statistics Canada
  * Railways, active and abandoned, from OpenStreetMap via Overpass (ODbL)
  * Daymet v4 daily precipitation and snow water equivalent (ORNL DAAC, public domain)
"""
import io
import json
import time
import zipfile

import arcpy
import pandas as pd
import requests
from osgeo import gdal
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (ACI_DIR, ACI_YEARS, BASINS, CLIMATE_END, CLIMATE_START, DEM_DIR,
                    DEM_RES, RAW, SR_WKID)

gdal.UseExceptions()
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
gdal.SetConfigOption("GDAL_HTTP_MAX_RETRY", "5")
arcpy.env.overwriteOutput = True

AAFC = "https://agriculture.canada.ca/atlas/data_donnees/watersheds2013/data_donnees/fgdb"
GEOMET = "https://api.weather.gc.ca/collections"
STAC = "https://datacube.services.geo.ca/stac/api/search"
ACI = "https://agriculture.canada.ca/imagery-images/rest/services/annual_crop_inventory/{}/ImageServer"
DAYMET = "https://daymet.ornl.gov/single-pixel/api/data"
NRN = "https://geo.statcan.gc.ca/nrn_rrn/sk/nrn_rrn_sk_GPKG.zip"
OVERPASS = "https://overpass-api.de/api/interpreter"

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=6, backoff_factor=3, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=None)))
# Overpass rejects the default python-requests agent
SESSION.headers["User-Agent"] = "Prairie-Wetland-Drainage-Spring-Flooding-ArcGIS-Pro (research)"


def aafc_layers():
    for name in ["TOTAL_GROSS_DRAIN", "TOTAL_EFF_DRAIN"]:
        dest = RAW / "aafc" / name
        if dest.exists():
            print(f"  already have {name}")
            continue
        r = SESSION.get(f"{AAFC}/HYD_AAFC_{name}_FGDB.zip", timeout=900)
        r.raise_for_status()
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(dest)
        print(f"  downloaded {name}")


def basin_boundaries():
    """Dissolve the AAFC gross drainage polygons of the study gauges into one feature each."""
    out = RAW / "basins.geojson"
    if out.exists():
        return out
    src = str(RAW / "aafc" / "TOTAL_GROSS_DRAIN" / "HYD_AAFC_TOTAL_GROSS_DRAIN.gdb"
              / "HYD_AAFC_TOTAL_GROSS_DRAIN")
    ids = ",".join(f"'{s}'" for s in BASINS)
    lyr = arcpy.management.MakeFeatureLayer(src, "gross", f"STATION_NUMBER IN ({ids})")[0]
    with arcpy.EnvManager(outputCoordinateSystem=arcpy.SpatialReference(SR_WKID)):
        diss = arcpy.management.Dissolve(lyr, "memory/basins", "STATION_NUMBER",
                                         [["TOTAL_GROSS_SQKM_AREA", "MAX"],
                                          ["TOTAL_EFFECTIVE_SQKM_AREA", "MAX"]])
    arcpy.conversion.FeaturesToJSON(diss, str(out), geoJSON="GEOJSON",
                                    outputToWGS84="KEEP_INPUT_SR")
    print(f"  {int(arcpy.management.GetCount(diss)[0])} basin boundaries")
    return out


def basin_features(geojson):
    return {f["properties"]["STATION_NUMBER"]: f for f in json.loads(geojson.read_text())["features"]
            if f["properties"]["STATION_NUMBER"] in BASINS}


def to_wgs84(feature):
    """GeoJSON geometry (EPSG:3979) as an arcpy geometry in WGS84; AsShape drops the CRS."""
    shape = arcpy.AsShape(feature["geometry"])
    return arcpy.Polygon(shape.getPart(), arcpy.SpatialReference(SR_WKID)).projectAs(
        arcpy.SpatialReference(4326))


def bbox_3979(feature):
    xs, ys = [], []
    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for x in c:
                walk(x)
    walk(feature["geometry"]["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def dem(station, feature):
    """Clip the HRDEM 2 m mosaic to the basin at its 4 m overview (Canada Atlas Lambert)."""
    out = DEM_DIR / f"{station}.tif"
    if out.exists():
        return
    xmin, ymin, xmax, ymax = bbox_3979(feature)
    geo = to_wgs84(feature).extent
    items = SESSION.post(STAC, json={"collections": ["hrdem-mosaic-2m"],
                                     "bbox": [geo.XMin, geo.YMin, geo.XMax, geo.YMax]},
                         timeout=120).json()["features"]
    srcs = ["/vsicurl/" + it["assets"]["dtm"]["href"] for it in items]
    cutline = DEM_DIR / f"{station}_cut.geojson"
    cutline.write_text(json.dumps({"type": "FeatureCollection", "features": [feature],
                                   "crs": {"type": "name",
                                           "properties": {"name": f"EPSG:{SR_WKID}"}}}))
    gdal.Warp(str(out), srcs, format="GTiff", outputBounds=(xmin, ymin, xmax, ymax),
              xRes=DEM_RES, yRes=DEM_RES, targetAlignedPixels=True, resampleAlg="average",
              cutlineDSName=str(cutline), cropToCutline=False, dstNodata=-32767,
              creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
              multithread=True, warpMemoryLimit=1024)
    # A dropped connection can leave the output silently empty, so check coverage
    valid_km2 = (gdal.Open(str(out)).ReadAsArray() > -1000).sum() * DEM_RES ** 2 / 1e6
    basin_km2 = arcpy.AsShape(feature["geometry"]).area / 1e6
    if valid_km2 < 0.95 * basin_km2:
        out.unlink()
        raise RuntimeError(f"DEM {station} only {valid_km2:.0f} of {basin_km2:.0f} km² - rerun")
    print(f"  DEM {station}: {len(srcs)} mosaic tile(s), {valid_km2:.0f} km² valid")


def aci(station, feature):
    """One 30 m crop-inventory GeoTIFF per year, in the project CRS."""
    xmin, ymin, xmax, ymax = bbox_3979(feature)
    w, h = int((xmax - xmin) / 30) + 1, int((ymax - ymin) / 30) + 1
    for year in ACI_YEARS:
        out = ACI_DIR / f"{station}_{year}.tif"
        if out.exists():
            continue
        params = {"bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": SR_WKID,
                  "imageSR": SR_WKID, "size": f"{w},{h}", "format": "tiff",
                  "pixelType": "U8", "interpolation": "RSP_NearestNeighbor", "f": "image"}
        r = SESSION.get(ACI.format(year) + "/exportImage", params=params, timeout=600)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            print(f"  ACI {year} unavailable for {station} ({r.status_code})")
            continue
        out.write_bytes(r.content)
    print(f"  ACI {station}: {len(list(ACI_DIR.glob(f'{station}_*.tif')))} years")


def roads():
    """National Road Network (Saskatchewan): used to remove road embankments from the DEM."""
    dest = RAW / "nrn"
    if dest.exists() and any(dest.glob("*.gpkg")):
        print("  already have NRN roads")
        return
    r = SESSION.get(NRN, timeout=900)
    r.raise_for_status()
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(dest)
    print("  downloaded NRN roads")


def rails(features):
    """OpenStreetMap railways, including abandoned lines whose embankments still dam valleys.

    (NRCan's National Railway Network download returns 403, and it omits abandoned lines.)
    """
    out = RAW / "rails.geojson"
    if out.exists():
        print("  already have rails")
        return
    exts = [to_wgs84(f).extent for f in features.values()]
    s, w = min(e.YMin for e in exts) - 0.05, min(e.XMin for e in exts) - 0.05
    n, e = max(e.YMax for e in exts) + 0.05, max(e.XMax for e in exts) + 0.05
    query = (f'[out:json][timeout:300];way["railway"~"^(rail|disused|abandoned|razed)$"]'
             f'({s},{w},{n},{e});out geom;')
    r = SESSION.post(OVERPASS, data={"data": query}, timeout=600)
    r.raise_for_status()
    feats = [{"type": "Feature", "properties": {"railway": el["tags"].get("railway")},
              "geometry": {"type": "LineString",
                           "coordinates": [[p["lon"], p["lat"]] for p in el["geometry"]]}}
             for el in r.json()["elements"] if len(el.get("geometry", [])) > 1]
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"  {len(feats):,} railway segments")


def daily_flow(stations):
    out = RAW / "daily_flow.csv"
    if out.exists():
        print("  already have daily_flow.csv")
        return
    frames = []
    for st in stations:
        rows, offset = [], 0
        while True:
            page = SESSION.get(f"{GEOMET}/hydrometric-daily-mean/items",
                               params={"STATION_NUMBER": st, "limit": 10000, "offset": offset,
                                       "properties": "DATE,DISCHARGE", "f": "json"},
                               timeout=300).json()
            rows += [f["properties"] for f in page["features"]]
            if len(page["features"]) < 10000:
                break
            offset += 10000
        df = pd.DataFrame(rows)
        df["STATION_NUMBER"] = st
        frames.append(df)
        print(f"  flow {st}: {df['DISCHARGE'].notna().sum():,} daily values "
              f"({df['DATE'].min()[:4]}-{df['DATE'].max()[:4]})")
    pd.concat(frames).to_csv(out, index=False)


def daymet(features):
    """Daily precipitation and SWE at each basin's centroid."""
    out = RAW / "daymet.csv"
    if out.exists():
        print("  already have daymet.csv")
        return
    frames = []
    for st, f in features.items():
        c = to_wgs84(f).centroid
        r = SESSION.get(DAYMET, params={"lat": round(c.Y, 4), "lon": round(c.X, 4),
                                        "vars": "prcp,swe",
                                        "start": f"{CLIMATE_START}-01-01",
                                        "end": f"{CLIMATE_END}-12-31"}, timeout=300)
        r.raise_for_status()
        text = r.text[r.text.index("year,yday"):]
        df = pd.read_csv(io.StringIO(text))
        df.columns = ["year", "yday", "prcp", "swe"]
        df["STATION_NUMBER"] = st
        frames.append(df)
        time.sleep(1)
    pd.concat(frames).to_csv(out, index=False)
    print(f"  Daymet for {len(frames)} basins")


if __name__ == "__main__":
    for d in (RAW, DEM_DIR, ACI_DIR):
        d.mkdir(parents=True, exist_ok=True)
    print("AAFC drainage areas")
    aafc_layers()
    feats = basin_features(basin_boundaries())
    print("Road and rail networks")
    roads()
    rails(feats)
    print("Streamflow")
    daily_flow(list(BASINS))
    print("Climate (Daymet)")
    daymet(feats)
    print("Crop inventory")
    for st, f in feats.items():
        aci(st, f)
    print("LiDAR DEM (largest download, ~20-40 min)")
    for st, f in feats.items():
        dem(st, f)
