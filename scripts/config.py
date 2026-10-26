"""Shared paths and parameters for the prairie wetland storage and spring runoff project."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DEM_DIR = RAW / "dem"
ACI_DIR = RAW / "aci"
GDB = ROOT / "data" / "wetland_storage.gdb"
OUTPUTS = ROOT / "outputs"
FIGURES = ROOT / "figures"
MAPS = ROOT / "maps"
APRX = ROOT / "Prairie_Wetland_Storage.aprx"

# NAD83(CSRS) / Canada Atlas Lambert: native CRS of the NRCan HRDEM mosaic
SR_WKID = 3979

# Water Survey of Canada gauges: Smith Creek plus neighbouring pothole basins with full
# LiDAR coverage, >= ~30 years of record, and no major regulation. Yorkton Creek near
# Ebenezer (05MB001) was dropped because it contains the Willow Brook basin.
BASINS = {
    "05ME007": "Smith Creek near Marchwell",
    "05MD010": "Stony Creek near Kamsack",
    "05MB005": "Willow Brook at Willowbrook",
    "05MB006": "Crooked Hill Creek near Canora",
    "05MB007": "Spirit Creek near Buchanan",
    "05MA011": "Birch Creek near Elfros",
    "05MA021": "Magnusson Creek near Wynyard",
    "05MA016": "Romance Creek near Watson",
    "05MA020": "Quill Creek near Quill Lake",
    "05MA012": "Ironspring Creek near Watson",
    "05JF011": "Cottonwood Creek near Lumsden",
    "05ND011": "Shepherd Creek near Alameda",
}
BENCHMARK = "05ME007"

DEM_RES = 4.0           # metres; the HRDEM mosaic's first overview level
ACI_YEARS = list(range(2009, 2026))
CLIMATE_START, CLIMATE_END = 1980, 2023   # Daymet v4 R1 coverage

# Depression thresholds
MIN_DEPTH_M = 0.10      # below LiDAR vertical noise
MIN_AREA_M2 = 1000      # 0.1 ha, ~ one 30 m crop-inventory pixel
MAX_DEPTH_M = 10.0      # deeper closed pits are gravel pits and borrow pits, not wetlands
