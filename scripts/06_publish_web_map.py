"""Publish the results to ArcGIS Online as a hosted feature layer and a web map.

Signs in through ArcGIS Pro's active portal connection (GIS("pro")): no username or
password is read or stored here. Sign in to ArcGIS Online in ArcGIS Pro first.
Items are created PRIVATE; share them from ArcGIS Online when ready.

Layers:
  Basins                basin outlines with storage, wetland status and runoff metrics
  Gauges                Water Survey of Canada stations
  Smith_Creek_Potholes  every depression in the benchmark basin, coloured by status
"""
import importlib
import shutil
import tempfile
from pathlib import Path

import arcpy
from arcgis.gis import GIS, ItemProperties, ItemTypeEnum
from arcgis.map import Map

from config import BENCHMARK, GDB

maps = importlib.import_module("05_make_maps")  # reuse the layout colours

TITLE = "Prairie Wetland Storage - Southeast Saskatchewan"
SNIPPET = ("Remaining prairie pothole storage from 4 m LiDAR, wetland status 2009-2025 and spring "
           "runoff trends for 12 gauged basins in southeast Saskatchewan.")
TAGS = ["Saskatchewan", "prairie pothole", "wetland drainage", "LiDAR", "spring runoff", "hydrology"]
LAYERS = {"Basins": "Basins", "Gauges": "Gauges",
          f"Depressions_{BENCHMARK}": "Smith_Creek_Potholes"}
STORAGE_BREAKS = [25, 50, 75, 125]


def color(hex_color, alpha=220):
    h = hex_color.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]


def fill(hex_color, outline="#4a4a4a", width=0.6, alpha=220):
    return {"type": "esriSFS", "style": "esriSFSSolid", "color": color(hex_color, alpha),
            "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": color(outline, 255),
                        "width": width}}


def package():
    """Copy the layers into a fresh file geodatabase and zip it for upload."""
    work = Path(tempfile.mkdtemp())
    gdb = arcpy.management.CreateFileGDB(str(work), "prairie_wetland_storage.gdb")[0]
    for src, name in LAYERS.items():
        arcpy.conversion.ExportFeatures(str(GDB / src), f"{gdb}/{name}")
    zip_path = shutil.make_archive(str(work / "prairie_wetland_storage"), "zip", work,
                                   "prairie_wetland_storage.gdb")
    return zip_path


def renderers():
    lows = [0] + STORAGE_BREAKS[:-1]
    basins = {"renderer": {
        "type": "classBreaks", "field": "STORAGE_MM", "minValue": 0,
        "classBreakInfos": [
            {"classMaxValue": hi, "label": f"{lo}–{hi} mm" if lo else f"< {hi} mm",
             "symbol": fill(c)}
            for lo, hi, c in zip(lows, STORAGE_BREAKS, maps.BLUES)]}}
    basins["renderer"]["classBreakInfos"][-1]["label"] = f"> {STORAGE_BREAKS[-2]} mm"
    potholes = {"renderer": {
        "type": "uniqueValue", "field1": "STATUS",
        "uniqueValueInfos": [{"value": k, "label": k, "symbol": fill(v, v, 0, 230)}
                             for k, v in maps.STATUS_COLORS.items()]}}
    gauges = {"renderer": {"type": "simple", "symbol": {
        "type": "esriSMS", "style": "esriSMSTriangle", "color": color("#0b0b0b", 255), "size": 9,
        "outline": {"color": color("#ffffff", 255), "width": 1}}}}
    return {"Basins": basins, "Gauges": gauges, "Smith_Creek_Potholes": potholes}


def main():
    gis = GIS("pro")
    me = gis.users.me
    print(f"Signed in to {gis.properties.portalName} as {me.username}")

    existing = gis.content.search(f'title:"{TITLE}" AND owner:{me.username}', max_items=10)
    if existing:
        for it in existing:
            print(f"  already exists: {it.type} {it.homepage}")
        raise SystemExit("Items with this title already exist; delete or rename them first.")

    zip_path = package()
    props = ItemProperties(title=TITLE, item_type=ItemTypeEnum.FILE_GEODATABASE, snippet=SNIPPET,
                           tags=TAGS)
    gdb_item = gis.content.folders.get().add(props, file=zip_path).result()
    service = gdb_item.publish()
    print(f"  hosted feature layer: {service.homepage}")

    m = Map()
    drawing = renderers()
    # Draw order: basins at the bottom, potholes, gauges on top
    for name in ["Basins", "Smith_Creek_Potholes", "Gauges"]:
        lyr = next(l for l in service.layers if l.properties.name == name)
        m.content.add(lyr, drawing_info=drawing[name])
    m.basemap.basemap = "gray-vector"
    webmap = m.save({"title": TITLE, "snippet": SNIPPET, "tags": TAGS})
    print(f"  web map (private): {webmap.homepage}")


if __name__ == "__main__":
    main()
