"""ArcGIS Pro Python toolbox: map closed depressions and their storage from a LiDAR DEM.

Packages the method from scripts/02_depressions.py so it can be run on any prairie
DEM: optional road-embankment removal, fill-difference depth, region grouping, and
per-depression area, depth and volume. Requires Spatial Analyst.
"""
import arcpy
from arcpy.sa import (Con, ExtractByAttributes, Fill, FocalStatistics, IsNull, NbrRectangle,
                      Raster, RegionGroup, SetNull, ZonalStatisticsAsTable)


class Toolbox:
    def __init__(self):
        self.label = "Depression Storage"
        self.alias = "depressionstorage"
        self.tools = [DepressionStorage]


class DepressionStorage:
    def __init__(self):
        self.label = "Map Depression Storage"
        self.description = ("Maps closed depressions (potholes) in a DEM and reports each one's "
                            "area, maximum depth and storage volume.")
        self.canRunInBackground = True

    def getParameterInfo(self):
        dem = arcpy.Parameter(displayName="Bare-earth DEM (projected, metres)", name="dem",
                              datatype="GPRasterLayer", parameterType="Required", direction="Input")
        roads = arcpy.Parameter(displayName="Roads (optional, removes embankments)", name="roads",
                                datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        roads.filter.list = ["Polyline"]
        road_buf = arcpy.Parameter(displayName="Road half-width (m)", name="road_buffer",
                                   datatype="GPDouble", parameterType="Optional", direction="Input")
        road_buf.value = 12
        depth = arcpy.Parameter(displayName="Minimum depth (m)", name="min_depth",
                                datatype="GPDouble", parameterType="Required", direction="Input")
        depth.value = 0.10
        area = arcpy.Parameter(displayName="Minimum area (m²)", name="min_area",
                               datatype="GPDouble", parameterType="Required", direction="Input")
        area.value = 1000
        max_depth = arcpy.Parameter(displayName="Maximum depth (m), deeper pits are excluded",
                                    name="max_depth", datatype="GPDouble", parameterType="Optional",
                                    direction="Input")
        max_depth.value = 10
        out = arcpy.Parameter(displayName="Output depressions", name="out_fc",
                              datatype="DEFeatureClass", parameterType="Required", direction="Output")
        return [dem, roads, road_buf, depth, area, max_depth, out]

    def isLicensed(self):
        return arcpy.CheckExtension("Spatial") == "Available"

    def execute(self, parameters, messages):
        p = {x.name: x for x in parameters}
        arcpy.CheckOutExtension("Spatial")
        dem = Raster(p["dem"].valueAsText)
        cell = dem.meanCellWidth
        cell_m2 = cell * cell
        arcpy.env.snapRaster, arcpy.env.cellSize, arcpy.env.extent = dem, dem, dem.extent
        # Keep every intermediate on the DEM's grid even if the roads are geographic
        arcpy.env.outputCoordinateSystem = dem.spatialReference

        if p["roads"].valueAsText:
            messages.addMessage("Removing road embankments...")
            buf = arcpy.analysis.PairwiseBuffer(p["roads"].valueAsText, "memory/roadbuf",
                                                f"{p['road_buffer'].value or 12} Meters", "ALL")
            arcpy.management.AddField(buf, "ON_ROAD", "SHORT")
            arcpy.management.CalculateField(buf, "ON_ROAD", "1")
            road_ras = f"{arcpy.env.scratchGDB}/roadras"
            arcpy.conversion.PolygonToRaster(buf, "ON_ROAD", road_ras, "CELL_CENTER", cellsize=dem)
            on_road = ~IsNull(Raster(road_ras))
            win = max(3, int(round(2 * (p["road_buffer"].value or 12) / cell)) + 5) | 1
            ditch = FocalStatistics(SetNull(on_road, dem), NbrRectangle(win, win, "CELL"),
                                    "MINIMUM", "DATA")
            dem = Con(on_road & ~IsNull(ditch), ditch, dem)

        messages.addMessage("Filling and grouping depressions...")
        depth = Fill(dem) - dem
        regions = RegionGroup(Con(depth >= p["min_depth"].value, 1), "EIGHT", "WITHIN", "NO_LINK")
        keep = ExtractByAttributes(regions, f"Count >= {int(p['min_area'].value / cell_m2)}")

        out = p["out_fc"].valueAsText
        arcpy.conversion.RasterToPolygon(keep, out, "NO_SIMPLIFY", "Value", "MULTIPLE_OUTER_PART")
        arcpy.management.AlterField(out, "gridcode", "DEP_ID", "Depression ID")
        ZonalStatisticsAsTable(keep, "Value", depth, "memory/zs", "DATA", "ALL")
        stats = {r[0]: r[1:] for r in arcpy.da.SearchCursor("memory/zs",
                                                            ["Value", "COUNT", "MAX", "SUM"])}
        for f in ("AREA_M2", "MAX_DEPTH_M", "VOLUME_M3"):
            arcpy.management.AddField(out, f, "DOUBLE")
        total_v = total_a = 0.0
        max_depth = p["max_depth"].value
        with arcpy.da.UpdateCursor(out, ["DEP_ID", "AREA_M2", "MAX_DEPTH_M", "VOLUME_M3"]) as cur:
            for dep, *_ in cur:
                count, mx, s = stats[dep]
                if max_depth and mx > max_depth:  # gravel or borrow pit, not a wetland
                    cur.deleteRow()
                    continue
                cur.updateRow([dep, count * cell_m2, mx, s * cell_m2])
                total_a += count * cell_m2
                total_v += s * cell_m2

        messages.addMessage(f"{int(arcpy.management.GetCount(out)[0]):,} depressions, "
                            f"{total_a / 1e6:,.2f} km² total area, {total_v / 1e6:,.2f} million m³ "
                            f"storage")
