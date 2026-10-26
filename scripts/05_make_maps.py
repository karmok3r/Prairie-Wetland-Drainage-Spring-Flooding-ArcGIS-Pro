"""Build Prairie_Wetland_Storage.aprx with four layouts and export them (PNG + PDF).

  1. Remaining pothole storage by basin (LiDAR)
  2. Trend in spring runoff ratio by basin, 1980-2023
  3. Smith Creek close-up: depressions by wetland status on a LiDAR hillshade
  4. Cottonwood Creek close-up (the basin with the most farmed-through storage)
"""
import json
import shutil
import sys
from pathlib import Path

import arcpy
import pandas as pd
import requests
from arcpy.sa import Hillshade, Raster

from config import APRX, BASINS, DEM_DIR, GDB, MAPS, OUTPUTS, RAW, ROOT, SR_WKID

arcpy.env.overwriteOutput = True
arcpy.CheckOutExtension("Spatial")

BLANK = r"C:\Program Files\ArcGIS\Pro\Resources\ArcToolBox\Services\routingservices\data\Blank.aprx"
SR = arcpy.SpatialReference(SR_WKID)
BLUES = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab"]
ORANGES = ["#fde0cf", "#f6a77f", "#eb6834", "#b8461b"]
STATUS_COLORS = {"Persistent wetland": "#1c5cab", "Intermittent wetland": "#86b6ef",
                 "Farmed through": "#eb6834", "Grassland / other": "#c3c2b7"}
CLOSEUPS = {"05ME007": "Smith Creek", "05JF011": "Cottonwood Creek"}
ZOOM_M = 6000  # width of the close-up window
SOURCES = ("Data: NRCan HRDEM LiDAR, AAFC Watersheds Project 2013 and Annual Crop Inventory "
           "2009-2025, Water Survey of Canada (HYDAT), Daymet v4. Author: Uday Karmoker, 2026.")


def rgb(hex_color, alpha=100):
    h = hex_color.lstrip("#")
    return {"RGB": [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]}


# ---------- data prep ----------
def gauges():
    out = str(GDB / "Gauges")
    cache = RAW / "stations.json"
    if not cache.exists() or not json.loads(cache.read_text())["features"]:
        # GeoMet ignores a comma-separated STATION_NUMBER list, so ask for one station at a time
        found = []
        for st in BASINS:
            r = requests.get("https://api.weather.gc.ca/collections/hydrometric-stations/items",
                             params={"STATION_NUMBER": st, "f": "json"}, timeout=120)
            r.raise_for_status()
            found += r.json()["features"]
        if len(found) < len(BASINS):
            sys.exit(f"Only {len(found)} of {len(BASINS)} gauges found; not caching")
        cache.write_text(json.dumps({"type": "FeatureCollection", "features": found}))
    feats = [f for f in json.loads(cache.read_text())["features"]
             if f["properties"]["STATION_NUMBER"] in BASINS]
    arcpy.management.CreateFeatureclass(str(GDB), "Gauges", "POINT", spatial_reference=SR)
    arcpy.management.AddField(out, "STATION_NUMBER", "TEXT", field_length=10)
    arcpy.management.AddField(out, "STATION_NAME", "TEXT", field_length=80)
    with arcpy.da.InsertCursor(out, ["STATION_NUMBER", "STATION_NAME", "SHAPE@"]) as ins:
        for f in feats:
            x, y = f["geometry"]["coordinates"][:2]
            pt = arcpy.PointGeometry(arcpy.Point(x, y), arcpy.SpatialReference(4326)).projectAs(SR)
            ins.insertRow([f["properties"]["STATION_NUMBER"],
                           f["properties"]["STATION_NAME"].title(), pt])
    return out


def add_basin_results():
    """Copy trend and runoff metrics onto the Basins feature class for mapping."""
    fc = str(GDB / "Basins")
    tr = pd.read_csv(OUTPUTS / "trends.csv", dtype={"STATION_NUMBER": str}).set_index("STATION_NUMBER")
    m = pd.read_csv(OUTPUTS / "basin_metrics.csv", dtype={"STATION_NUMBER": str}).set_index("STATION_NUMBER")
    fields = {"SEN_SLOPE": ("DOUBLE", lambda s: tr.loc[s, "sen_slope_per_decade"]),
              "TREND_P": ("DOUBLE", lambda s: tr.loc[s, "p_value"]),
              "RUNOFF_RATIO": ("DOUBLE", lambda s: m.loc[s, "median_runoff_ratio"]),
              "FARMED_PCT": ("DOUBLE", lambda s: m.loc[s, "farmed_storage_pct"]),
              "SHORT_NAME": ("TEXT", lambda s: BASINS[s].split(" near ")[0].split(" at ")[0])}
    existing = [f.name for f in arcpy.ListFields(fc)]
    for name, (ftype, _) in fields.items():
        if name not in existing:
            arcpy.management.AddField(fc, name, ftype, field_length=40 if ftype == "TEXT" else None)
    with arcpy.da.UpdateCursor(fc, ["STATION_NUMBER"] + list(fields)) as cur:
        for row in cur:
            cur.updateRow([row[0]] + [fn(row[0]) for _, fn in fields.values()])
    return fc


def zoom_window(station):
    """A ZOOM_M square around the densest part of the basin's depressions."""
    deps = str(GDB / f"Depressions_{station}")
    xs, ys = zip(*[r[0] for r in arcpy.da.SearchCursor(deps, ["SHAPE@XY"])])
    sx, sy = sorted(xs), sorted(ys)
    cx, cy = sx[len(sx) // 2], sy[len(sy) // 2]  # median depression location
    h = ZOOM_M / 2
    return arcpy.Extent(cx - h, cy - h, cx + h, cy + h, spatial_reference=SR)


def hillshade(station, ext):
    out = ROOT / "data" / "regions" / f"hillshade_{station}.tif"
    pad = ZOOM_M * 0.4  # the map frame is wider than the square window
    big = arcpy.Extent(ext.XMin - pad, ext.YMin - pad, ext.XMax + pad, ext.YMax + pad)
    with arcpy.EnvManager(extent=big, snapRaster=str(DEM_DIR / f"{station}.tif")):
        Hillshade(Raster(str(DEM_DIR / f"{station}.tif")), 315, 45, "NO_SHADOWS", 3).save(str(out))
    return out


def zoom_polygons(windows):
    fc = str(GDB / "Zoom_Areas")
    arcpy.management.CreateFeatureclass(str(GDB), "Zoom_Areas", "POLYGON", spatial_reference=SR)
    arcpy.management.AddField(fc, "STATION_NUMBER", "TEXT", field_length=10)
    with arcpy.da.InsertCursor(fc, ["STATION_NUMBER", "SHAPE@"]) as ins:
        for st, e in windows.items():
            ins.insertRow([st, arcpy.Polygon(arcpy.Array([e.lowerLeft, e.upperLeft, e.upperRight,
                                                          e.lowerRight]), SR)])
    return fc


# ---------- symbology ----------
def clear_heading(lyr):
    cim = lyr.getDefinition("V3")
    if hasattr(cim.renderer, "heading"):
        cim.renderer.heading = ""
    for g in getattr(cim.renderer, "groups", None) or []:
        g.heading = ""
    lyr.setDefinition(cim)


def outline(lyr, color, width, label=None):
    sym = lyr.symbology
    sym.renderer.symbol.color = {"RGB": [0, 0, 0, 0]}
    sym.renderer.symbol.outlineColor = rgb(color)
    sym.renderer.symbol.outlineWidth = width
    if label:
        sym.renderer.label = label
    lyr.symbology = sym


def graduated(lyr, field, uppers, labels, colors):
    sym = lyr.symbology
    sym.updateRenderer("GraduatedColorsRenderer")
    sym.renderer.classificationField = field
    sym.renderer.breakCount = len(uppers)
    for brk, up in zip(sym.renderer.classBreaks, uppers):
        brk.upperBound = up
    lyr.symbology = sym
    sym = lyr.symbology
    for brk, color in zip(sym.renderer.classBreaks, colors):
        brk.symbol.color = rgb(color)
        brk.symbol.outlineColor = rgb("#52514e")
        brk.symbol.outlineWidth = 0.8
    lyr.symbology = sym
    cim = lyr.getDefinition("V3")
    for brk, label in zip(cim.renderer.breaks, labels):
        brk.label = label
    lyr.setDefinition(cim)
    clear_heading(lyr)


def by_status(lyr):
    sym = lyr.symbology
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = ["STATUS"]
    lyr.symbology = sym
    sym = lyr.symbology
    for item in sym.renderer.groups[0].items:
        val = item.values[0][0]
        item.symbol.color = rgb(STATUS_COLORS.get(val, "#c3c2b7"), 90)
        item.symbol.outlineColor = rgb("#ffffff", 0)
    lyr.symbology = sym
    clear_heading(lyr)


def labels(lyr, expression, size=9):
    lyr.showLabels = True
    lc = lyr.listLabelClasses()[0]
    lc.expressionEngine = "Arcade"
    lc.expression = expression
    cim = lyr.getDefinition("V3")
    ts = cim.labelClasses[0].textSymbol.symbol
    ts.height = size
    halo = arcpy.cim.CreateCIMObjectFromClassName("CIMPolygonSymbol", "V3")
    fill = arcpy.cim.CreateCIMObjectFromClassName("CIMSolidFill", "V3")
    fill.color = arcpy.cim.CreateCIMObjectFromClassName("CIMRGBColor", "V3")
    fill.color.values = [255, 255, 255, 100]
    halo.symbolLayers = [fill]
    ts.haloSymbol, ts.haloSize = halo, 1.2
    # Neighbouring basins are small at this scale; let labels move outside their polygon
    mx = cim.labelClasses[0].maplexLabelPlacementProperties
    mx.canPlaceLabelOutsidePolygon = True
    mx.neverRemoveLabel = True
    lyr.setDefinition(cim)


def gauge_symbol(lyr):
    sym = lyr.symbology
    sym.renderer.symbol.applySymbolFromGallery("Triangle 1")
    sym.renderer.symbol.color = rgb("#0b0b0b")
    sym.renderer.symbol.size = 8
    sym.renderer.label = "Streamflow gauge"
    lyr.symbology = sym


# ---------- layouts ----------
def new_map(aprx, name):
    m = aprx.createMap(name, "MAP")
    m.spatialReference = SR
    for lyr in m.listLayers():
        m.removeLayer(lyr)
    try:
        m.addBasemap("Light Gray Canvas")
    except Exception as e:
        print(f"  basemap skipped: {e}")
    return m


def layout(aprx, name, title, subtitle, m, extent, legend_skip=(), inset=None):
    lyt = aprx.createLayout(11, 8.5, "INCH", name)
    mf = lyt.createMapFrame(arcpy.Extent(0.35, 0.9, 7.85, 7.55), m, "Map Frame")
    mf.camera.setExtent(extent)
    aprx.createTextElement(lyt, arcpy.Point(0.4, 8.05), "POINT", title, 20, name="Title")
    aprx.createTextElement(lyt, arcpy.Point(0.4, 7.72), "POINT", subtitle, 10, name="Subtitle")
    leg = lyt.createMapSurroundElement(arcpy.Point(8.1, 7.55), "LEGEND", mf, name="Legend")
    leg.elementWidth = 2.6
    leg.title = ""
    for item in leg.items:
        if item.name in legend_skip or item.name.startswith(("World", "Light Gray")):
            item.visible = False
    cim = lyt.getDefinition("V3")
    for el in cim.elements:
        if type(el).__name__ == "CIMLegend":
            for item in el.items:
                item.showHeading = False
    lyt.setDefinition(cim)
    na = lyt.createMapSurroundElement(
        arcpy.Point(8.3, 2.3), "NORTH_ARROW", mf,
        aprx.listStyleItems("ArcGIS 2D", "NORTH_ARROW", "ArcGIS North 1")[0])
    na.elementHeight = 0.5
    bar = aprx.listStyleItems("ArcGIS 2D", "SCALE_BAR", "Scale Line 1 Metric")
    sb = lyt.createMapSurroundElement(arcpy.Point(8.1, 1.5), "SCALE_BAR", mf, bar[0] if bar else None)
    sb.elementWidth = 2.4
    aprx.createTextElement(lyt, arcpy.Point(0.4, 0.55), "POINT", SOURCES, 7, name="Credits")
    if inset:
        inset_map, inset_ext = inset
        f = lyt.createMapFrame(arcpy.Extent(8.1, 3.0, 10.6, 5.2), inset_map, "Inset")
        f.camera.setExtent(inset_ext)
    return lyt


def export(lyt, stem):
    MAPS.mkdir(exist_ok=True)
    lyt.exportToPNG(str(MAPS / f"{stem}.png"), resolution=200)
    lyt.exportToPDF(str(MAPS / f"{stem}.pdf"), resolution=300)
    print(f"  exported maps/{stem}.png/.pdf")


def padded(fc, pad=0.08, where=None):
    lyr = arcpy.management.MakeFeatureLayer(fc, f"ext_{abs(hash(where))}", where)[0]
    e = arcpy.Describe(lyr).extent
    dx, dy = (e.XMax - e.XMin) * pad, (e.YMax - e.YMin) * pad
    # Extra room at the bottom keeps the southernmost basin clear of the basemap credits
    return arcpy.Extent(e.XMin - dx, e.YMin - 2.5 * dy, e.XMax + dx, e.YMax + dy)


def main(aprx_path=APRX):
    aprx_path = Path(aprx_path)
    try:
        aprx_path.unlink(missing_ok=True)
    except PermissionError:
        sys.exit(f"{aprx_path.name} is open in ArcGIS Pro. Close it and rerun.")
    shutil.copy(BLANK, aprx_path)
    aprx = arcpy.mp.ArcGISProject(str(aprx_path))
    aprx.defaultGeodatabase = str(GDB)
    for item in aprx.listMaps() + aprx.listLayouts():
        aprx.deleteItem(item)

    basins = add_basin_results()
    gauge_fc = gauges()
    windows = {st: zoom_window(st) for st in CLOSEUPS}
    zooms = zoom_polygons(windows)
    region = padded(basins)

    # 1. Storage overview
    m = new_map(aprx, "Pothole Storage")
    b = m.addDataFromPath(basins)
    b.name = "Remaining pothole storage"
    graduated(b, "STORAGE_MM", [25, 50, 75, 125],
              ["< 25 mm", "25–50 mm", "50–75 mm", "> 75 mm"], BLUES)
    labels(b, '$feature.SHORT_NAME + TextFormatting.NewLine + Round($feature.STORAGE_MM, 0) + " mm"', 7.5)
    g = m.addDataFromPath(gauge_fc)
    g.name = "Gauges"
    gauge_symbol(g)
    export(layout(aprx, "01 Storage", "Remaining pothole storage in southeast Saskatchewan",
                  "Water that closed depressions can hold before spilling, from 4 m NRCan LiDAR "
                  "(mm over the basin)", m, region), "01_pothole_storage")

    # 2. Runoff trend
    m = new_map(aprx, "Runoff Trend")
    b = m.addDataFromPath(basins)
    b.name = "Change in spring runoff ratio per decade"
    graduated(b, "SEN_SLOPE", [0.01, 0.02, 0.03, 0.05],
              ["< 0.01", "0.01–0.02", "0.02–0.03", "> 0.03"], ORANGES)
    labels(b, 'var s = Round($feature.SEN_SLOPE, 3); var sig = IIf($feature.TREND_P < 0.05, "", " (n.s.)");'
              ' $feature.SHORT_NAME + TextFormatting.NewLine + "+" + s + sig', 7.5)
    g = m.addDataFromPath(gauge_fc)
    g.name = "Gauges"
    gauge_symbol(g)
    export(layout(aprx, "02 Runoff Trend", "Spring runoff is rising faster than snowpack",
                  "Theil-Sen trend in spring runoff ratio (runoff ÷ snowpack + spring precipitation), "
                  "1980–2023. n.s. = not significant", m, region), "02_runoff_trend")

    # 3-4. Close-ups
    for i, (st, short) in enumerate(CLOSEUPS.items(), start=3):
        ext = windows[st]
        m = new_map(aprx, f"{short} Close-up")
        hs = m.addDataFromPath(str(hillshade(st, ext)))
        hs.name = "LiDAR hillshade"
        hs_sym = hs.symbology
        hs_sym.colorizer.colorRamp = aprx.listColorRamps("Black to White")[0]
        hs.symbology = hs_sym
        hs.transparency = 30
        d = m.addDataFromPath(str(GDB / f"Depressions_{st}"))
        d.name = "Depression status (2009–2025)"
        by_status(d)
        inset = aprx.createMap(f"{short} Locator", "MAP")
        inset.spatialReference = SR
        for lyr in inset.listLayers():
            inset.removeLayer(lyr)
        ctx = inset.addDataFromPath(basins)
        ctx_sym = ctx.symbology
        ctx_sym.renderer.symbol.color = rgb("#e1e0d9")
        ctx_sym.renderer.symbol.outlineColor = rgb("#898781")
        ctx_sym.renderer.symbol.outlineWidth = 0.4
        ctx.symbology = ctx_sym
        bl = inset.addDataFromPath(basins)
        bl.definitionQuery = f"STATION_NUMBER = '{st}'"
        outline(bl, "#0b0b0b", 1.2)
        zl = inset.addDataFromPath(zooms)
        zl.definitionQuery = f"STATION_NUMBER = '{st}'"
        outline(zl, "#d03b3b", 1.5)
        s = pd.read_csv(OUTPUTS / "wetland_status_summary.csv",
                        dtype={"STATION_NUMBER": str}).set_index("STATION_NUMBER").loc[st]
        stem = f"0{i}_{short.lower().replace(' ', '_')}_closeup"
        export(layout(aprx, f"0{i} {short}", f"{short}: potholes and how they are used",
                      f"6 × 6 km window. {s.farmed_storage_pct}% of the basin's pothole storage is "
                      f"farmed through (never wet, cropped ≥ 75% of years)", m, ext,
                      legend_skip=("LiDAR hillshade",),
                      inset=(inset, padded(basins, 0.05))), stem)

    aprx.save()
    print(f"  saved {aprx_path.name}")


if __name__ == "__main__":
    main(*sys.argv[1:2])
