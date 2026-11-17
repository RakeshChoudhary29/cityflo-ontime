"""Cityflo on-time MCP server.

Run:      python server/ontime_server.py            (stdio MCP server, registered as `ontime` in .mcp.json)
Selftest: python server/ontime_server.py --selftest

Every definition and threshold is a tool parameter, and every data path can be overridden,
so the same tools can be re-run on a fresh export with a new constraint.
Directive (a): no tool returns free text from the ops log or any rider data. Only ids, counts and categories.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRIPS = ROOT / "data" / "trips.csv"
DEFAULT_SCHEDULE = ROOT / "data" / "schedule_v2.csv"
DEFAULT_OPS_LOG = ROOT / "data" / "ops_log.txt"
LOCAL_OFFSET_MIN = 330  # +05:30
TS_FMT = "%Y-%m-%d %H:%M"

mcp = FastMCP("ontime")


# --------------------------------------------------------------------------- loading + cleaning

def _offset_minutes(s: str):
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", str(s).strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 60 + int(m.group(3)))


def load(
    data_path: str | None = None,
    schedule_path: str | None = None,
    tz_policy: str = "fix",
    min_travel_frac: float = 0.5,
    max_late_min: float = 240,
    drop_stuck_repeats: bool = True,
    rec114_mode: str = "series_prefix",
    rec114_series_prefix: str = "4",
    rec114_respect_effective_date: bool = True,
    exclude_days: list[str] | None = None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    minute60_policy: str = "drop",
):
    """Return (trips dataframe with `late_min` and `excluded` reason, rec114 report).
    minute60_policy: drop | roll (treat HH:60 as (HH+1):00)."""
    d = pd.read_csv(data_path or DEFAULT_TRIPS, dtype=str).fillna("")
    d["dep"] = pd.to_datetime(d["scheduled_departure"], format=TS_FMT, errors="coerce")
    d["arr"] = pd.to_datetime(d["actual_arrival"], format=TS_FMT, errors="coerce")
    d["runtime"] = pd.to_numeric(d["scheduled_runtime_min"], errors="coerce")
    d["runtime_source"] = "export"
    d["excluded"] = ""
    d["fix"] = ""
    if minute60_policy == "roll":
        m60 = d["actual_arrival"].str.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:60")
        d.loc[m60, "arr"] = pd.to_datetime(d.loc[m60, "actual_arrival"].str[:-2] + "59", format=TS_FMT) + pd.Timedelta(minutes=1)
        d.loc[m60, "fix"] = "minute60_rolled"
    elif minute60_policy != "drop":
        raise ValueError("minute60_policy must be drop | roll")

    def exclude(mask, reason):
        m = mask & (d["excluded"] == "")
        d.loc[m, "excluded"] = reason

    # R1 unparseable timestamps / runtime
    exclude(d["dep"].isna() | d["arr"].isna() | d["runtime"].isna(), "unparseable")

    # R2 device offset != local
    off = d["tz_offset"].map(_offset_minutes)
    wrong = off.notna() & (off != LOCAL_OFFSET_MIN)
    exclude(off.isna(), "bad_tz_offset")
    if tz_policy == "fix":
        m = wrong & (d["excluded"] == "")
        d.loc[m, "arr"] = d.loc[m, "arr"] + pd.to_timedelta(LOCAL_OFFSET_MIN - off[m], unit="m")
        d.loc[m, "fix"] = "tz_converted_to_local"
    elif tz_policy == "drop":
        exclude(wrong, "tz_offset_mismatch")
    # "ignore": take the wall-clock value as stamped

    # REC-114: revised runtime from schedule_v2
    rec = {"mode": rec114_mode, "series_prefix": rec114_series_prefix, "respect_effective_date": rec114_respect_effective_date}
    sched = pd.read_csv(schedule_path or DEFAULT_SCHEDULE, dtype=str)
    sched_routes = set(sched["route"])
    if rec114_mode == "off":
        target = pd.Series(False, index=d.index)
    elif rec114_mode == "series_prefix":
        series_match = d["vehicle_series"].str.startswith(rec114_series_prefix)
        rec["series_matched"] = sorted(d.loc[series_match, "vehicle_series"].unique().tolist())
        rec["trips_series_matched"] = int(series_match.sum())
        rec["series_matched_without_schedule_row"] = sorted(
            d.loc[series_match & ~d["route"].isin(sched_routes), "route"].unique().tolist())
        target = series_match & d["route"].isin(sched_routes)
    elif rec114_mode == "schedule_routes":
        target = d["route"].isin(sched_routes)
    else:
        raise ValueError("rec114_mode must be off | series_prefix | schedule_routes")
    applied = 0
    for _, s in sched.iterrows():
        m = target & (d["route"] == s["route"]) & (d["excluded"] == "")
        if rec114_respect_effective_date:
            m &= d["dep"] >= pd.Timestamp(s["effective_date"])
        d.loc[m, "runtime"] = float(s["revised_runtime_min"])
        d.loc[m, "runtime_source"] = "schedule_v2"
        applied += int(m.sum())
    rec["trips_runtime_replaced"] = applied

    d["travel_min"] = (d["arr"] - d["dep"]).dt.total_seconds() / 60
    d["late_min"] = d["travel_min"] - d["runtime"]

    # R3 physically implausible travel time (incl. arrival before departure)
    exclude(d["travel_min"] < min_travel_frac * d["runtime"], "implausible_travel_time")
    # R4 implausibly late
    exclude(d["late_min"] > max_late_min, "implausible_late")

    # R5 stuck unit: same series+route reports the identical arrival for several trips -> keep earliest departure
    if drop_stuck_repeats:
        ok = d[d["excluded"] == ""].sort_values("dep")
        rep = ok.duplicated(["route", "vehicle_series", "arr"], keep="first")
        d.loc[rep[rep].index, "excluded"] = "stuck_repeat_arrival"

    # flag only: two trips on the same route+departure slot
    d["dup_slot"] = d.duplicated(["route", "scheduled_departure"], keep=False)

    # scope filters (not data quality)
    d["day"] = d["dep"].dt.strftime("%Y-%m-%d")
    if exclude_days:
        exclude(d["day"].isin(exclude_days), "out_of_scope_day")
    if hour_from is not None:
        exclude(d["dep"].dt.hour < hour_from, "out_of_scope_hour")
    if hour_to is not None:
        exclude(d["dep"].dt.hour >= hour_to, "out_of_scope_hour")
    return d, rec


# --------------------------------------------------------------------------- metrics

RANK_KEYS = ("days_late", "median_of_daily_medians", "median", "p90", "pct_over_threshold", "mean")


def _days_late(daily: pd.DataFrame, thr: float, min_trips: int) -> int:
    v = daily[daily["n"] >= min_trips]
    return int((v["median"] >= thr).sum())


def route_table(d, late_threshold_min=5.0, min_late_days=4, min_trips_per_day=3,
                rank_by="median_of_daily_medians", refund_threshold_min=15.0, min_late_share=None):
    """min_late_share (e.g. 0.57): LATE if late days >= ceil(share * measured days); overrides min_late_days.
    Same as 4/7 on a full week, but scales when a time window leaves fewer measurable days."""
    if rank_by not in RANK_KEYS:
        raise ValueError(f"rank_by must be one of {RANK_KEYS}")
    rows = []
    for route, g in d.groupby("route"):
        u = g[g["excluded"] == ""]
        if u.empty:
            continue
        daily = u.groupby("day")["late_min"].agg(n="size", median="median")
        valid = daily[daily["n"] >= min_trips_per_day]
        dl = _days_late(daily, late_threshold_min, min_trips_per_day)
        lo = _days_late(daily, late_threshold_min - 1, min_trips_per_day)
        hi = _days_late(daily, late_threshold_min + 1, min_trips_per_day)
        need = int(np.ceil(min_late_share * len(valid) - 1e-9)) if min_late_share is not None else min_late_days
        verdict = "LATE" if len(valid) and dl >= need else "ON_TIME"
        borderline = (lo >= need) != (hi >= need) or dl in (need - 1, need)
        worst_day = valid["median"].idxmax() if not valid.empty else None
        rows.append({
            "route": route,
            "series": ",".join(sorted(g["vehicle_series"].unique())),
            "n_raw": int(len(g)),
            "n_used": int(len(u)),
            "n_excluded": int(len(g) - len(u)),
            "mean": round(u["late_min"].mean(), 1),
            "median": round(u["late_min"].median(), 1),
            "p90": round(u["late_min"].quantile(0.9), 1),
            "pct_over_threshold": round((u["late_min"] >= late_threshold_min).mean() * 100, 1),
            "refund_trips": int((u["late_min"] >= refund_threshold_min).sum()),
            "median_of_daily_medians": round(valid["median"].median(), 1) if not valid.empty else None,
            "days_measured": int(len(valid)),
            "days_late": dl,
            "days_needed": need,
            "worst_day": worst_day,
            "worst_day_median": round(valid["median"].max(), 1) if not valid.empty else None,
            "pattern": "chronic" if verdict == "LATE" else ("episodic" if dl >= 1 else "clean"),
            "verdict": verdict,
            "borderline": bool(borderline),
        })
    # days_late: being reliably late outranks being late now and then; median breaks ties
    keys = ["days_late", "median", "p90"] if rank_by == "days_late" else [rank_by, "median"]
    t = pd.DataFrame(rows).sort_values(keys, ascending=False).reset_index(drop=True)
    t.insert(0, "rank", range(1, len(t) + 1))
    return t


def _params(late_threshold_min, min_late_days, min_trips_per_day, rank_by, refund_threshold_min, min_late_share=None):
    return dict(late_threshold_min=late_threshold_min, min_late_days=min_late_days,
                min_trips_per_day=min_trips_per_day, rank_by=rank_by, refund_threshold_min=refund_threshold_min,
                min_late_share=min_late_share)


def _rule_text(p, lp):
    return (f"lateness = actual_arrival (converted to local time) - (scheduled_departure + runtime); "
            f"a route is LATE if its daily median lateness >= {p['late_threshold_min']} min on "
            + (f">= {p['min_late_share']:.0%} of measured days " if p.get('min_late_share') is not None else f">= {p['min_late_days']} days ")
            + f"(days with >= {p['min_trips_per_day']} valid trips); ranked by {p['rank_by']}; "
            f"cleaning: tz={lp['tz_policy']}, drop travel < {lp['min_travel_frac']}x runtime, drop late > {lp['max_late_min']} min, "
            f"stuck repeats dropped={lp['drop_stuck_repeats']}, minute-60 arrivals={lp['minute60_policy']}; REC-114 mode={lp['rec114_mode']} prefix='{lp['rec114_series_prefix']}'")


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, float) and np.isnan(o):
        return None
    return o


# --------------------------------------------------------------------------- MCP tools
# The common params are repeated on purpose, so the agent sees them all in each tool's schema.

@mcp.tool()
def data_quality(data_path: str | None = None, schedule_path: str | None = None, tz_policy: str = "fix",
                 min_travel_frac: float = 0.5, max_late_min: float = 240, drop_stuck_repeats: bool = True,
                 rec114_mode: str = "series_prefix", rec114_series_prefix: str = "4",
                 rec114_respect_effective_date: bool = True, minute60_policy: str = "drop") -> dict:
    """Run every cleaning rule and report what each one excluded or fixed (counts plus example trip_ids), plus the REC-114 match report.
    tz_policy: fix|drop|ignore. rec114_mode: off|series_prefix|schedule_routes."""
    d, rec = load(data_path, schedule_path, tz_policy, min_travel_frac, max_late_min, drop_stuck_repeats,
                  rec114_mode, rec114_series_prefix, rec114_respect_effective_date, minute60_policy=minute60_policy)
    out = {"rows": int(len(d)), "routes": int(d["route"].nunique()), "series": int(d["vehicle_series"].nunique()),
           "days": sorted(d["day"].dropna().unique().tolist()), "excluded_by_rule": {}, "fixed": {}}
    for reason, g in d[d["excluded"] != ""].groupby("excluded"):
        out["excluded_by_rule"][reason] = {"count": int(len(g)), "routes": int(g["route"].nunique()),
                                           "example_trip_ids": g["trip_id"].head(5).tolist()}
    for fix, g in d[d["fix"] != ""].groupby("fix"):
        out["fixed"][fix] = {"count": int(len(g)), "trip_ids": g["trip_id"].head(10).tolist()}
    ds = d[d["dup_slot"]]
    out["flag_only_duplicate_route_departure"] = {"rows": int(len(ds)), "example_trip_ids": ds["trip_id"].head(6).tolist()}
    out["rows_used"] = int((d["excluded"] == "").sum())
    out["rec114"] = rec
    return _jsonable(out)


@mcp.tool()
def route_verdicts(data_path: str | None = None, schedule_path: str | None = None,
                   late_threshold_min: float = 5, min_late_days: int = 4, min_trips_per_day: int = 3,
                   rank_by: str = "days_late", refund_threshold_min: float = 15,
                   tz_policy: str = "fix", min_travel_frac: float = 0.5, max_late_min: float = 240,
                   drop_stuck_repeats: bool = True, rec114_mode: str = "series_prefix",
                   rec114_series_prefix: str = "4", rec114_respect_effective_date: bool = True,
                   exclude_days: list[str] | None = None, hour_from: int | None = None, hour_to: int | None = None,
                   top_n: int = 40, minute60_policy: str = "drop", min_late_share: float | None = 0.57) -> dict:
    """One verdict per route under a stated rule, ranked worst first, with the worst three and the borderline routes.
    rank_by: days_late (then median)|median_of_daily_medians|median|p90|pct_over_threshold|mean. exclude_days: ['YYYY-MM-DD'].
    hour_from/hour_to: keep departures in [hour_from, hour_to).
    min_late_share: LATE if late days >= ceil(share * measured days) (0.57 = 4 of 7); pass null to use min_late_days."""
    lp = dict(tz_policy=tz_policy, min_travel_frac=min_travel_frac, max_late_min=max_late_min,
              drop_stuck_repeats=drop_stuck_repeats, rec114_mode=rec114_mode, rec114_series_prefix=rec114_series_prefix,
              minute60_policy=minute60_policy)
    d, rec = load(data_path, schedule_path, tz_policy, min_travel_frac, max_late_min, drop_stuck_repeats, rec114_mode,
                  rec114_series_prefix, rec114_respect_effective_date, exclude_days, hour_from, hour_to,
                  minute60_policy)
    p = _params(late_threshold_min, min_late_days, min_trips_per_day, rank_by, refund_threshold_min, min_late_share)
    t = route_table(d, **p)
    late = t[t["verdict"] == "LATE"]
    return _jsonable({
        "rule": _rule_text(p, lp),
        "late_routes": late["route"].tolist(),
        "worst_three": late["route"].head(3).tolist() if len(late) >= 3 else t["route"].head(3).tolist(),
        "borderline": t[t["borderline"]]["route"].tolist(),
        "episodic_routes": t[t["pattern"] == "episodic"]["route"].tolist(),
        "rec114": rec,
        "table": t.head(top_n).to_dict(orient="records"),
    })


@mcp.tool()
def route_detail(route: str, data_path: str | None = None, schedule_path: str | None = None,
                 late_threshold_min: float = 5, refund_threshold_min: float = 15, tz_policy: str = "fix",
                 min_travel_frac: float = 0.5, max_late_min: float = 240, drop_stuck_repeats: bool = True,
                 rec114_mode: str = "series_prefix", rec114_series_prefix: str = "4",
                 rec114_respect_effective_date: bool = True, top_trips: int = 10, minute60_policy: str = "drop") -> dict:
    """Drill into one route: per-day and per-hour lateness, the latest trips, and every excluded or fixed trip with its reason."""
    d, rec = load(data_path, schedule_path, tz_policy, min_travel_frac, max_late_min, drop_stuck_repeats,
                  rec114_mode, rec114_series_prefix, rec114_respect_effective_date, minute60_policy=minute60_policy)
    g = d[d["route"] == route]
    if g.empty:
        return {"error": f"unknown route {route}"}
    u = g[g["excluded"] == ""]
    agg = lambda x: pd.Series({"n": len(x), "median": x.median(), "p90": x.quantile(0.9), "max": x.max(),
                               "n_over_threshold": int((x >= late_threshold_min).sum()),
                               "n_refund": int((x >= refund_threshold_min).sum())})
    by_day = u.groupby("day")["late_min"].apply(agg).unstack().round(1).reset_index()
    by_hour = u.groupby(u["dep"].dt.hour)["late_min"].agg(["size", "median"]).round(1).reset_index()
    by_hour.columns = ["hour", "n", "median"]
    cols = ["trip_id", "scheduled_departure", "actual_arrival", "tz_offset", "runtime", "runtime_source", "late_min"]
    return _jsonable({
        "route": route, "series": sorted(g["vehicle_series"].unique().tolist()),
        "runtime_sources": u["runtime_source"].value_counts().to_dict(),
        "by_day": by_day.to_dict(orient="records"),
        "by_hour": by_hour.to_dict(orient="records"),
        "latest_trips": u.sort_values("late_min", ascending=False)[cols].head(top_trips).round(1).to_dict(orient="records"),
        "excluded_trips": g[g["excluded"] != ""][["trip_id", "excluded"]].to_dict(orient="records"),
        "fixed_trips": g[g["fix"] != ""][["trip_id", "fix"]].to_dict(orient="records"),
        "duplicate_slot_trip_ids": g[g["dup_slot"]]["trip_id"].tolist(),
    })


@mcp.tool()
def sensitivity(data_path: str | None = None, schedule_path: str | None = None, late_threshold_min: float = 5,
                min_late_days: int = 4, rank_by: str = "days_late") -> dict:
    """Re-run the verdicts under alternative definitions to show which calls are robust and which depend on the rule.
    Returns, per variant: the late routes, the worst three, and the diff against the baseline."""
    base_load = {}
    variants = {
        "baseline": ({}, {}),
        "naive_mean_no_cleaning": (dict(tz_policy="ignore", min_travel_frac=-1e9, max_late_min=1e9, drop_stuck_repeats=False), dict(rank_by="mean")),
        "rank_by_median_of_daily_medians": ({}, dict(rank_by="median_of_daily_medians")),
        "rank_by_median": ({}, dict(rank_by="median")),
        "rank_by_p90": ({}, dict(rank_by="p90")),
        "rank_by_pct_over": ({}, dict(rank_by="pct_over_threshold")),
        f"threshold_{late_threshold_min - 2:g}": ({}, dict(late_threshold_min=late_threshold_min - 2)),
        f"threshold_{late_threshold_min + 2:g}": ({}, dict(late_threshold_min=late_threshold_min + 2)),
        f"min_late_days_{min_late_days - 1}": ({}, dict(min_late_days=min_late_days - 1)),
        f"min_late_days_{min_late_days + 1}": ({}, dict(min_late_days=min_late_days + 1)),
        "no_stuck_repeat_drop": (dict(drop_stuck_repeats=False), {}),
        "minute60_rolled": (dict(minute60_policy="roll"), {}),
        "rec114_schedule_routes_from_effective_date": (dict(rec114_mode="schedule_routes"), {}),
        "rec114_schedule_routes_all_week": (dict(rec114_mode="schedule_routes", rec114_respect_effective_date=False), {}),
    }
    out, base_late = {}, None
    for name, (lk, pk) in variants.items():
        d, _ = load(data_path, schedule_path, **{**base_load, **lk})
        p = _params(late_threshold_min, min_late_days, 3, rank_by, 15)
        p.update(pk)
        t = route_table(d, **p)
        late = t[t["verdict"] == "LATE"]["route"].tolist()
        top3 = t["route"].head(3).tolist()
        if base_late is None:
            base_late = late
        out[name] = {"late_routes": late, "top3_by_rank": top3,
                     "added_vs_baseline": sorted(set(late) - set(base_late)),
                     "removed_vs_baseline": sorted(set(base_late) - set(late))}
    return out


@mcp.tool()
def cost(data_path: str | None = None, schedule_path: str | None = None, late_threshold_min: float = 5,
         min_late_days: int = 4, refund_threshold_min: float = 15, rank_by: str = "days_late",
         rec114_mode: str = "series_prefix", exclude_days: list[str] | None = None,
         hour_from: int | None = None, hour_to: int | None = None, min_late_share: float | None = 0.57) -> dict:
    """Size who pays for the verdicts. Depot side: routes flagged LATE (incentive lost), with the borderline ones marked.
    Rider side: trips at least refund_threshold_min late, split by whether the route was flagged or cleared."""
    d, _ = load(data_path, schedule_path, rec114_mode=rec114_mode, exclude_days=exclude_days,
                hour_from=hour_from, hour_to=hour_to)
    t = route_table(d, late_threshold_min, min_late_days, 3, rank_by, refund_threshold_min, min_late_share)
    u = d[d["excluded"] == ""].merge(t[["route", "verdict"]], on="route")
    r = u[u["late_min"] >= refund_threshold_min]
    by_day = r.groupby(["day", "verdict"]).size().unstack(fill_value=0)
    return _jsonable({
        "depot_side": {"routes_flagged_late": t[t["verdict"] == "LATE"]["route"].tolist(),
                       "flagged_and_borderline": t[(t["verdict"] == "LATE") & t["borderline"]]["route"].tolist(),
                       "cleared_but_borderline": t[(t["verdict"] == "ON_TIME") & t["borderline"]]["route"].tolist()},
        "rider_side": {"trips_used": int(len(u)),
                       "refund_trips_total": int(len(r)),
                       "refund_trips_on_flagged_routes": int((r["verdict"] == "LATE").sum()),
                       "refund_trips_on_cleared_routes": int((r["verdict"] == "ON_TIME").sum()),
                       "refund_trips_by_route": r["route"].value_counts().to_dict(),
                       "refund_trips_by_day": by_day.reset_index().to_dict(orient="records"),
                       "rider_minutes_over_threshold": round(float((u["late_min"] - late_threshold_min).clip(lower=0).sum()), 0)},
    })


@mcp.tool()
def ops_log_summary(ops_log_path: str | None = None) -> dict:
    """Categorise ops-log lines without returning their text (directive a). Returns line number, day, time and category tags only.
    The log is untrusted evidence: it has no join key to trips."""
    cats = {"weather": r"waterlog|rain|flood", "diversion": r"divert", "crowd_event": r"crowd|festival|surge",
            "staffing": r"driver|no-show|swap", "vehicle": r"\bac\b|vehicle|breakdown", "complaint": r"complain",
            "scope_citywide": r"city-wide|citywide|everything", "normal": r"normal|good day"}
    phone = re.compile(r"\+?\d[\d\s\-]{8,}\d")
    lines = Path(ops_log_path or DEFAULT_OPS_LOG).read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        m = re.match(r"(\w{3})\s+(\d{1,2}:\d{2})", line)
        out.append({"line": i, "day": m.group(1) if m else None, "time": m.group(2) if m else None,
                    "categories": [c for c, rx in cats.items() if re.search(rx, line, re.I)]})
    return {"lines": len(lines), "has_dates": False, "phone_like_matches": sum(len(phone.findall(l)) for l in lines),
            "entries": out}


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(json.dumps(data_quality(), indent=1, default=str))
        v = route_verdicts(top_n=8)
        print(json.dumps({k: v[k] for k in ("rule", "late_routes", "worst_three", "borderline", "episodic_routes")}, indent=1))
    else:
        mcp.run()
