"""Spring runoff response and its relation to remaining pothole storage.

Per basin and year (1980-2023):
  runoff_mm     total discharge 1 Mar - 30 Jun divided by gross drainage area. Prairie
                gauges are often seasonal; missing March days are treated as frozen (0 flow)
                and a year is kept only if 1 Apr - 30 Jun is >= 90% complete.
  water_in_mm   peak snow water equivalent (1 Jan - 30 Apr) + precipitation 1 Apr - 30 Jun
                (Daymet, basin centroid)
  runoff_ratio  runoff_mm / water_in_mm

Basin level (n = 12): Spearman correlation of the median runoff ratio with LiDAR storage,
depression area, AAFC effective-area fraction and the share of storage that is farmed through.
Trend: Theil-Sen slope and Mann-Kendall (Kendall's tau vs year) of the runoff ratio per basin.

Outputs: outputs/basin_years.csv, basin_metrics.csv, correlations.csv, trends.csv;
         figures/storage_vs_runoff.png, figures/runoff_trends.png, figures/smith_creek_runoff.png
"""
import arcpy
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import kendalltau, spearmanr, theilslopes

from config import BASINS, BENCHMARK, CLIMATE_END, CLIMATE_START, FIGURES, GDB, OUTPUTS, RAW

BLUE, INK, INK2, GRID, ORANGE = "#2a78d6", "#0b0b0b", "#52514e", "#e1e0d9", "#eb6834"
plt.rcParams.update({"font.family": "Segoe UI", "font.size": 10, "axes.edgecolor": "#c3c2b7",
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb"})


def basin_years():
    gross = {s: a for s, a in arcpy.da.SearchCursor(str(GDB / "Basins"),
                                                     ["STATION_NUMBER", "GROSS_KM2"])}
    q = pd.read_csv(RAW / "daily_flow.csv", dtype={"STATION_NUMBER": str}, parse_dates=["DATE"])
    q = q[q["STATION_NUMBER"].isin(BASINS)]
    q["year"], q["month"] = q["DATE"].dt.year, q["DATE"].dt.month
    spring = q[q["month"].between(3, 6)]
    amj = spring[spring["month"] >= 4]
    complete = amj.groupby(["STATION_NUMBER", "year"])["DISCHARGE"].count() >= 0.9 * 91
    vol = spring.groupby(["STATION_NUMBER", "year"])["DISCHARGE"].agg(
        flow_sum="sum", peak_cms="max")
    vol = vol[complete.reindex(vol.index, fill_value=False)]
    vol["runoff_mm"] = [v * 86400 / (gross[s] * 1e6) * 1000
                        for (s, _), v in zip(vol.index, vol["flow_sum"])]

    c = pd.read_csv(RAW / "daymet.csv", dtype={"STATION_NUMBER": str})
    c["date"] = pd.to_datetime(c["year"].astype(str), format="%Y") + pd.to_timedelta(c["yday"] - 1, "D")
    c["month"] = c["date"].dt.month
    swe = c[c["month"] <= 4].groupby(["STATION_NUMBER", "year"])["swe"].max()
    rain = c[c["month"].between(4, 6)].groupby(["STATION_NUMBER", "year"])["prcp"].sum()
    clim = pd.DataFrame({"peak_swe_mm": swe, "spring_precip_mm": rain})
    clim["water_in_mm"] = clim["peak_swe_mm"] + clim["spring_precip_mm"]

    df = vol.join(clim, how="inner").reset_index()
    df = df[df["year"].between(CLIMATE_START, CLIMATE_END)]
    df["runoff_ratio"] = df["runoff_mm"] / df["water_in_mm"]
    df.to_csv(OUTPUTS / "basin_years.csv", index=False)
    print(f"  {len(df)} basin-years ({df.groupby('STATION_NUMBER').size().min()}-"
          f"{df.groupby('STATION_NUMBER').size().max()} per basin)")
    return df


def basin_metrics(df):
    dep = pd.read_csv(OUTPUTS / "depression_summary.csv", dtype={"STATION_NUMBER": str})
    wet = pd.read_csv(OUTPUTS / "wetland_status_summary.csv", dtype={"STATION_NUMBER": str})
    aafc = pd.DataFrame(arcpy.da.TableToNumPyArray(
        str(GDB / "Basins"), ["STATION_NUMBER", "GROSS_KM2", "EFFECTIVE_KM2"]))
    aafc["effective_pct"] = 100 * aafc["EFFECTIVE_KM2"] / aafc["GROSS_KM2"]
    resp = df.groupby("STATION_NUMBER").agg(
        years=("year", "size"), median_runoff_mm=("runoff_mm", "median"),
        median_runoff_ratio=("runoff_ratio", "median"), median_water_in_mm=("water_in_mm", "median"))
    m = (dep.merge(wet[["STATION_NUMBER", "farmed_storage_pct", "wetland_area_pct_of_basin"]],
                   on="STATION_NUMBER")
            .merge(aafc[["STATION_NUMBER", "effective_pct"]], on="STATION_NUMBER")
            .merge(resp, left_on="STATION_NUMBER", right_index=True))
    m.round(3).to_csv(OUTPUTS / "basin_metrics.csv", index=False)
    return m


def correlations(m):
    rows = []
    for x, label in [("storage_mm", "LiDAR depression storage (mm)"),
                     ("depression_area_pct", "Depression area (% of basin)"),
                     ("farmed_storage_pct", "Share of storage farmed through (%)"),
                     ("effective_pct", "AAFC effective area (% of gross)")]:
        rho, p = spearmanr(m[x], m["median_runoff_ratio"])
        rows.append({"variable": label, "spearman_rho": round(rho, 2), "p_value": round(p, 3),
                     "n": len(m)})
    out = pd.DataFrame(rows)
    out.to_csv(OUTPUTS / "correlations.csv", index=False)
    print(out.to_string(index=False))
    return out


def trends(df):
    rows = []
    for st, d in df.groupby("STATION_NUMBER"):
        d = d.sort_values("year")
        slope, _, lo, hi = theilslopes(d["runoff_ratio"], d["year"])
        tau, p = kendalltau(d["year"], d["runoff_ratio"])
        rows.append({"STATION_NUMBER": st, "basin": BASINS[st], "years": len(d),
                     "sen_slope_per_decade": round(10 * slope, 4),
                     "ci_low": round(10 * lo, 4), "ci_high": round(10 * hi, 4),
                     "kendall_tau": round(tau, 2), "p_value": round(p, 3)})
    out = pd.DataFrame(rows).sort_values("sen_slope_per_decade")
    out.to_csv(OUTPUTS / "trends.csv", index=False)
    print(out.to_string(index=False))
    return out


def fig_scatter(m, corr):
    fig, ax = plt.subplots(figsize=(7, 4.6))
    ax.scatter(m["storage_mm"], m["median_runoff_ratio"], s=60, color=BLUE,
               edgecolor="#fcfcfb", linewidth=1.5, zorder=3)
    # Stony and Romance Creek plot almost on top of each other; put Romance's label below-left
    left = {"05MA016"}
    for _, r in m.iterrows():
        name = r["basin"].split(" near ")[0].split(" at ")[0]
        is_left = r["STATION_NUMBER"] in left
        ax.annotate(name, (r["storage_mm"], r["median_runoff_ratio"]),
                    xytext=(-6, -12) if is_left else (6, 4), ha="right" if is_left else "left",
                    textcoords="offset points", fontsize=8.5,
                    color=ORANGE if r["STATION_NUMBER"] == BENCHMARK else INK)
    c = corr.iloc[0]
    ax.set_title(f"Spearman ρ = {c.spearman_rho}, p = {c.p_value}, n = {c.n}",
                 loc="left", fontsize=9.5, color=INK2)
    ax.set_xlabel("Remaining pothole storage from LiDAR (mm of water over the basin)")
    ax.set_ylabel("Median spring runoff ratio\n(runoff ÷ snowpack + spring precipitation)")
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIGURES / "storage_vs_runoff.png", dpi=200)
    plt.close(fig)


def fig_trends(tr):
    t = tr.sort_values("sen_slope_per_decade")
    names = [b.split(" near ")[0].split(" at ")[0] for b in t["basin"]]
    fig, ax = plt.subplots(figsize=(7, 4.6))
    y = np.arange(len(t))
    ax.axvline(0, color="#898781", linewidth=1)
    ax.hlines(y, t["ci_low"], t["ci_high"], color="#86b6ef", linewidth=3)
    sig = t["p_value"] < 0.05
    ax.scatter(t["sen_slope_per_decade"], y, s=50, zorder=3,
               color=[BLUE if s else "#fcfcfb" for s in sig], edgecolor=BLUE, linewidth=1.5)
    ax.set_yticks(y, names, fontsize=9)
    ax.set_xlabel("Change in spring runoff ratio per decade (Theil-Sen, 95% CI)")
    ax.set_title("Filled = significant (Mann-Kendall p < 0.05)", loc="left", fontsize=9.5,
                 color=INK2)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    fig.tight_layout()
    fig.savefig(FIGURES / "runoff_trends.png", dpi=200)
    plt.close(fig)


def fig_smith(df):
    d = df[df["STATION_NUMBER"] == BENCHMARK].sort_values("year")
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.bar(d["year"], d["runoff_mm"], color=BLUE, width=0.7, label="Spring runoff (mm)")
    ax.plot(d["year"], d["water_in_mm"], color=ORANGE, linewidth=2,
            label="Snowpack + spring precipitation (mm)")
    ax.set_ylabel("mm over the basin")
    ax.legend(frameon=False, loc="upper left", fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIGURES / "smith_creek_runoff.png", dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    FIGURES.mkdir(exist_ok=True)
    print("Basin-years")
    df = basin_years()
    m = basin_metrics(df)
    print("Correlations with median runoff ratio")
    corr = correlations(m)
    print("Trends in runoff ratio")
    tr = trends(df)
    fig_scatter(m, corr)
    fig_trends(tr)
    fig_smith(df)
