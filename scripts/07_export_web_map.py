"""Export the results as GeoJSON for the free web map in docs/ (served by GitHub Pages).

Basins, gauges and the Smith Creek potholes are projected to WGS84, simplified to keep the
page light, and written with only the fields the map shows.
"""
import json

import arcpy

from config import BENCHMARK, GDB, ROOT

OUT = ROOT / "docs" / "data"
WGS84 = arcpy.SpatialReference(4326)
DECIMALS = 5  # ~1 m at this latitude

LAYERS = {
    "basins": ("Basins", "5 Meters",
               ["STATION_NUMBER", "SHORT_NAME", "BASIN_NAME", "GROSS_KM2", "N_DEPRESSIONS",
                "DEPRESSION_AREA_PCT", "STORAGE_MM", "FARMED_PCT", "RUNOFF_RATIO", "SEN_SLOPE",
                "TREND_P"]),
    "gauges": ("Gauges", None, ["STATION_NUMBER", "STATION_NAME"]),
    # Outlines come from 4 m cells; simplifying at one cell width removes the stair steps
    "smith_creek_potholes": (f"Depressions_{BENCHMARK}", "4 Meters",
                             ["DEP_ID", "STATUS", "AREA_M2", "VOLUME_M3", "MAX_DEPTH_M",
                              "WET_YEARS", "N_YEARS"]),
}


def rounded(coords):
    if isinstance(coords[0], (int, float)):
        return [round(c, DECIMALS) for c in coords[:2]]
    return [rounded(c) for c in coords]


def export(name, src, tolerance, fields):
    fc = str(GDB / src)
    if tolerance:
        fc = arcpy.cartography.SimplifyPolygon(fc, f"memory/{name}_simple", "POINT_REMOVE",
                                               tolerance, collapsed_point_option="NO_KEEP")[0]
    features = []
    with arcpy.da.SearchCursor(fc, fields + ["SHAPE@"], spatial_reference=WGS84) as cur:
        for *values, shape in cur:
            if shape is None:
                continue
            geo = shape.__geo_interface__  # Polygon / MultiPolygon / Point, rings correctly nested
            geometry = {"type": geo["type"], "coordinates": rounded(geo["coordinates"])}
            props = {f: round(v, 4) if isinstance(v, float) else v for f, v in zip(fields, values)}
            features.append({"type": "Feature", "properties": props, "geometry": geometry})
    path = OUT / f"{name}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                               separators=(",", ":")))
    print(f"  {name}: {len(features):,} features, {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    arcpy.env.overwriteOutput = True
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (src, tol, fields) in LAYERS.items():
        export(name, src, tol, fields)
