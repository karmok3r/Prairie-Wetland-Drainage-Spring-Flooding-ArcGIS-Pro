"""Run the whole pipeline with ArcGIS Pro's Python, keeping Windows awake until it finishes.

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe" run_all.py [--force]

--force recomputes every basin in step 02 (needed after changing the depression method);
without it, basins finished by an earlier interrupted run are reused.
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

STEPS = ["01_download_data", "02_depressions", "03_wetland_status", "04_runoff_analysis",
         "05_make_maps"]
SCRIPTS = Path(__file__).parent / "scripts"
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

if __name__ == "__main__":
    # A laptop going to sleep mid-run looks exactly like a hung geoprocessing tool
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    extra = ["--force"] if "--force" in sys.argv else []
    try:
        for step in STEPS:
            print(f"=== {step}", flush=True)
            t = time.time()
            args = [sys.executable, "-u", f"{step}.py"] + (extra if step == "02_depressions" else [])
            if subprocess.run(args, cwd=SCRIPTS).returncode:
                sys.exit(f"Pipeline failed in {step}")
            print(f"  ({step} took {(time.time() - t) / 60:.1f} min)", flush=True)
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
