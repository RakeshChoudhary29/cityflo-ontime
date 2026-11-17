# Route lateness verdicts: week of 2026-08-03 to 2026-08-09

**For:** Priya (Ops Lead, Mumbai North) · **Data:** `trips.csv` (5,292 trips, 40 routes), `schedule_v2.csv`, `ops_log.txt`
**Every number below comes from the `ontime` MCP server** (`server/ontime_server.py`). Re-run with the defaults to reproduce it.

## The answer

**Three routes were late this week: R-14, R-33 and R-26, ranked in that order. The other 37 were on time.**
R-35 had one very bad day (Wednesday, about 52 min late all day). It is **cleared as a route**, and **all 19 of its Wednesday trips go to the refund queue**.

| # | Route | Verdict | Days late (of 7) | Median late (min) | p90 (min) | Trips ≥ 5 min late | Refund-level trips (≥ 15 min) |
|---|---|---|---|---|---|---|---|
| 1 | **R-14** | **LATE** | 7 | 8.0 | 13.4 | 80% | 9 |
| 2 | **R-33** | **LATE** (borderline) | 6 | 5.0 | 10.0 | 62% | 1 |
| 3 | **R-26** | **LATE** (borderline) | 4 | 4.0 | 9.0 | 50% | 0 |

## The rule (re-runnable on next week's data)

1. **Lateness of a trip** = `actual_arrival` (converted to IST using `tz_offset`) − (`scheduled_departure` + `scheduled_runtime_min` from the export).
2. **Cleaning.** These run in order. Each rule is named and logged with trip ids by `data_quality`.
   - Drop arrivals that can't be parsed. This week that was 24 rows, all the placeholder value `08:60`.
   - Convert a non-`+05:30` offset to IST (1 row, T1-02864: 332 min early becomes 2 min early).
   - Drop trips whose travel time is under 0.5× the scheduled runtime: 18 rows, all arriving 5 min *before* departure.
   - Drop trips more than 240 min late (0 rows this week).
   - Stuck unit: when trips on the same route and series report an identical arrival, keep the earliest departure and drop the rest (59 rows).
   - 5,292 rows → **5,191 used**.
3. **REC-114:** revised runtimes from `schedule_v2.csv` apply only to trips whose `vehicle_series` starts with `4` **and** whose route has a `schedule_v2` row, from that row's `effective_date`. **This week that is 0 trips.** The only 4x series is `40` (R-40), and `schedule_v2` has no R-40 row.
4. **A route is LATE** if its **daily median lateness is ≥ 5 min on ≥ 57% of measured days**, rounded up. A measured day is one with at least 3 valid trips. On a full week that is **4 of 7**.
   - I use a share rather than a fixed 4 days so the rule still means the same thing when the review narrows the window. With a 06:00–10:00 window, some routes have only 5 measurable days, and "4 days" would quietly become 80%.
   - This week the share and the fixed 4 days give identical verdicts, both full-week and morning-peak (`min_late_share=0.57` vs `min_late_share=null, min_late_days=4`).
5. **Ranking:** days late first, then median, then p90.
6. **Refund line:** a trip ≥ 15 min late goes to the refund queue, **whatever its route's verdict**.

Command: `route_verdicts()` with its defaults (`late_threshold_min=5, min_late_share=0.57, rank_by="days_late", refund_threshold_min=15, rec114_mode="series_prefix"`).

---

## The calls, and how to defend each one

### 1. R-14 is #1 LATE

- **Rule:** late on 7 of 7 days. Daily medians were 7 / 9 / 9 / 6 / 9 / 7 / 9. Median 8 min, p90 13.4. 94 of 117 trips were ≥ 5 min late.
- **Case it breaks on:** **REC-114.** `schedule_v2` revises R-14 to 53 min from 08-05. If R-14's buses are really class 4xx (for example 414, since the data only stores the last two digits), the directive applies. Applied, R-14's lateness disappears **on exactly the effective date**: Tue median 9 → Wed **−4**, Thu −2, Sat −1, and it is cleared (`route_detail("R-14", rec114_mode="schedule_routes")`). I keep the rule anyway, for three reasons:
  - The data identifies R-14 as series `14`, not 4xx.
  - The directive is keyed on vehicle series, and the only 4x series has no revised row.
  - A timetable revision that turns a route from 9 min late to 4 min early overnight looks like absorbing the delay, not reconciling it.

  R-14 survives every other sensitivity variant: threshold 3 or 7, 3 or 5 required days, any ranking metric, cleaning on or off.
- **Cost and who pays:** the R-14 depot head loses his on-time incentive. R-14 also produced 9 refund-level trips, the most of any route except R-35's one bad day. Clearing it via REC-114 would leave 5 of those trips (Mon 3, Sun 2) on a route declared on time, and the riders would keep standing an extra 8 min every day with no follow-up.
- **What would change my mind:** a fleet register showing R-14 runs class-4xx vehicles. In that case REC-114 applies by its own terms and R-14 is cleared from 08-05. I would still report Mon–Tue (medians 7 and 9) as late.

### 2. R-33 is #2 LATE (borderline)

- **Rule:** late on 6 of 7 days (only Mon at 4.0 is under). Median 5, p90 10. 64 of 104 trips were ≥ 5 min late.
- **Case it breaks on:** **4 of its 6 late days sit exactly on the line at 5.0** (Tue, Wed, Fri, Sun). With "> 5" instead of "≥ 5" it is late on 2 days (Thu 7, Sat 8) and **cleared**, and a 7-min threshold also clears it (`sensitivity`). I keep it because the line is stated as inclusive, lateness is measured in whole minutes, and a route at or over 5 min on 6 of 7 days, with 62% of trips over, is reliably late. Ranking it above R-26 follows the same logic. Being late on 6 days is worse for riders than being slightly later on 4.
- **Cost and who pays:** the R-33 depot head loses the incentive over a margin of 0 min on 4 days. Riders: 64 trips ≥ 5 min late, 1 refund-level trip (T1-04286, +15).
- **What would change my mind:** next week's daily medians at 5.0 or below on 4 or more days, which would put it on the other side of the line, or a device-clock check showing R-33's units read about 1 min fast.

### 3. R-26 is #3 LATE (borderline, my weakest call)

- **Rule:** late on exactly 4 of 7 days (Mon 5.5, Wed 6.0, Thu 5.5, Sun 6.0). Tue, Fri and Sat were 3.0–3.5.
- **Case it breaks on:** each late day clears the line by only 0.5–1 min, and **no R-26 trip reached refund level** (the worst was T1-03314 at +13). A 6-min threshold clears it, and so does requiring 5 days. In trip terms it is almost the same as R-37, which is cleared (50% vs 48% of trips ≥ 5 min late). The two differ only in how those trips fall across days. I keep it because the 5-min, 4-day line was not moved after seeing R-26's result. Moving the line to clear one route is the same move as bending REC-114 to clear R-14. Its lateness is also concentrated in the evening peak (16:00–18:00 medians of 6.5–7), so it isn't random noise.
- **Cost and who pays:** the R-26 depot head loses the incentive while riders lost 0 refund-level trips. This is the call most likely to cost Priya the depot head's goodwill. Riders: 50 of 101 trips ≥ 5 min late.
- **R-26 is an evening-peak problem.** Morning peak only (`route_verdicts(hour_from=6, hour_to=10)`):

  | Route | Morning-peak days late / measured | Morning-peak median | Morning-peak verdict | Full-day verdict |
  |---|---|---|---|---|
  | R-26 | 2 / 6 (needs 4) | 4.0 | on time | LATE |
  | R-37 | 4 / 5 (needs 3) | 5.0 | **LATE** | on time |
  | R-14 | 6 / 7 | 9.0 | LATE | LATE |
  | R-33 | 6 / 6 | 6.5 | LATE | LATE |

  R-26 and R-37 swap places. R-26's mornings run about 3 min late, but 16:00–18:00 runs 6.5–7 min late. R-37 is the reverse, with its 06:00 hour at a median of 6. R-14 and R-33 are late in either window. So if the review cares only about morning commute, R-26 drops out and R-37 replaces it. The depot fix for R-26 is the evening timetable, not the whole route.
- **What would change my mind:** **late on fewer than 57% of measured days next week (fewer than 4 of 7) means it is cleared.**

### 4. R-35 is cleared, and Wednesday is refunded

- **Rule:** late on 1 of 7 days. Wednesday median 52, every other day 0–2. A route is LATE only when the delay repeats (≥ 4 days).
- **Case it breaks on:** R-35 has the **worst rider week of all 40 routes**: 19 trips 36–61 min late (T1-04521 at +61), and its p90 of 50.1 is the highest in the network. Ranked by p90 or by mean (the naive approach), it is #1 or #2 worst. My rule calls it ON_TIME while R-26, with zero refund-level trips, is LATE. I keep it because the verdict is about whether the route is chronically late, which is what the depot incentive pays for. The rider harm is paid directly through refunds whatever the verdict is.
- **Was Wednesday city-wide?** No. On a Wednesday-only run every other route was 0–2 min above its weekly median (R-14 9 vs 8, R-26 6 vs 4, R-19 5 vs 3). The ops log's "everything slow city-wide" line (line 4) doesn't match the data. The log also has no dates and HANDOFF calls it *last week's*, so it is not used to excuse or flag anything.
- **Is it a device fault?** Probably not. The 19 delays range from 36 to 61 min and spread from 06:35 to 20:21, while a clock offset would be constant.
- **Cost and who pays:** **19 refunds** paid from the rider refund queue. The R-35 depot head keeps the incentive.
- **What would change my mind:** another day at ≥ 5 min next week means it is flagged. If Wednesday is traced to a device fault, the refunds are reviewed.

### 5. REC-114 is not applied this week

- **Rule:** see rule 3. 154 trips match series prefix `4` (all R-40) and 0 runtimes are replaced.
- **Case it breaks on:** R-14 and R-27 are the only routes `schedule_v2` revises, and neither is 4xx. Applying the file by route (`rec114_mode="schedule_routes"`) **removes R-14 from the late list** and moves R-27 into the top three by ranking (`sensitivity`). It also makes R-27 *worse* (its median goes from 2 to 4.5), because a flat 45 min replaces runtimes of 35–55.
- **Cost and who pays:** reading the directive literally costs the R-14 depot head the incentive. Reading it loosely costs R-14's riders and Priya's credibility if the review asks why the worst route vanished on a mid-week timetable change.
- **What would change my mind:** a fleet register mapping R-14 or R-27 to class-4xx vehicles, or a `schedule_v2` row for R-40.

### 6. Cleaning rules

- **Rule:** as listed above. 101 rows excluded, 1 fixed.
- **Case it breaks on:** the stuck-repeat rule keeps the *earliest* trip and drops the later one, whose real arrival was probably later than the stale stamp. That probably **understates** lateness on the 59 dropped trips. I keep it because keeping the stale stamp would make those trips look early. Rolling the `08:60` rows forward to 09:00 instead of dropping them was tested: 21 of 24 become impossible, and 3 become fake +95 to +116 min delays (T1-03641, T1-03733, T1-04466). **No verdict changes under any cleaning variant** (`sensitivity`: `no_stuck_repeat_drop`, `minute60_rolled`, `naive_mean_no_cleaning` all give the same late set).
- **Cost and who pays:** none in the verdicts. The refund queue loses the 3 fake trips.
- **What would change my mind:** a cleaning variant that changes the late set, or an `08:60` that isn't a placeholder (a varied time, for example).

## Cost summary (`cost()`)

| Who pays | This week |
|---|---|
| Depot heads losing the incentive | **3**: R-14 (solid), R-33 and R-26 (borderline) |
| Refund queue, trips ≥ 15 min late | **36 in total.** 10 on flagged routes (R-14 9, R-33 1), **26 on cleared routes**: R-35 Wednesday 19, plus 1 each on R-05, R-08, R-09, R-11, R-24, R-27, R-38 |
| Borderline routes cleared | R-37 (2 of 7 days, 48% of trips ≥ 5 min, 0 refund trips) |

## All 40 verdicts

Ranked by days late, then median. Lateness in minutes. "Used / raw" is trips after cleaning / before.

| # | Route | Verdict | Days late | Median | p90 | Refund trips | Used / raw |
|---|---|---|---|---|---|---|---|
| 1 | R-14 | **LATE** | 7 | 8.0 | 13.4 | 9 | 117/119 |
| 2 | R-33 | **LATE** | 6 | 5.0 | 10.0 | 1 | 104/105 |
| 3 | R-26 | **LATE** | 4 | 4.0 | 9.0 | 0 | 101/105 |
| 4 | R-37 | on time (borderline) | 2 | 4.0 | 9.0 | 0 | 104/105 |
| 5 | R-19 | on time | 2 | 3.0 | 7.0 | 0 | 122/126 |
| 6 | R-31 | on time | 1 | 4.0 | 8.0 | 0 | 111/112 |
| 7 | R-15 | on time | 1 | 3.0 | 8.0 | 0 | 118/119 |
| 8 | R-17 | on time | 1 | 3.0 | 8.0 | 0 | 144/147 |
| 9 | R-34 | on time | 1 | 3.0 | 8.0 | 0 | 124/126 |
| 10 | R-35 | on time, **Wednesday refunded** | 1 | 2.0 | 50.1 | 19 | 130/133 |
| 11 | R-05 | on time | 1 | 2.0 | 7.0 | 1 | 103/105 |
| 12 | R-07 | on time | 0 | 3.0 | 7.4 | 0 | 147/147 |
| 13 | R-08 | on time | 0 | 3.0 | 7.0 | 1 | 126/126 |
| 14 | R-40 | on time | 0 | 3.0 | 7.0 | 0 | 151/154 |
| 15 | R-27 | on time | 0 | 2.0 | 8.0 | 1 | 143/147 |
| 16 | R-09 | on time | 0 | 2.0 | 7.7 | 1 | 144/147 |
| 17 | R-11 | on time | 0 | 2.0 | 7.7 | 1 | 134/140 |
| 18 | R-03 | on time | 0 | 2.0 | 7.0 | 0 | 103/105 |
| 19 | R-18 | on time | 0 | 2.0 | 7.0 | 0 | 153/154 |
| 20 | R-20 | on time | 0 | 2.0 | 7.0 | 0 | 140/147 |
| 21 | R-21 | on time | 0 | 2.0 | 7.0 | 0 | 122/126 |
| 22 | R-02 | on time | 0 | 2.0 | 6.0 | 0 | 104/105 |
| 23 | R-12 | on time | 0 | 2.0 | 6.0 | 0 | 111/112 |
| 24 | R-22 | on time | 0 | 2.0 | 6.0 | 0 | 112/112 |
| 25 | R-39 | on time | 0 | 2.0 | 6.0 | 0 | 153/154 |
| 26 | R-25 | on time | 0 | 1.0 | 7.0 | 0 | 138/140 |
| 27 | R-32 | on time | 0 | 1.0 | 7.0 | 0 | 131/133 |
| 28 | R-38 | on time | 0 | 1.0 | 7.0 | 1 | 146/147 |
| 29 | R-10 | on time | 0 | 1.0 | 6.0 | 0 | 125/126 |
| 30 | R-24 | on time | 0 | 1.0 | 6.0 | 1 | 144/147 |
| 31 | R-28 | on time | 0 | 1.0 | 5.1 | 0 | 150/154 |
| 32 | R-36 | on time | 0 | 1.0 | 5.1 | 0 | 130/133 |
| 33 | R-01 | on time | 0 | 1.0 | 5.0 | 0 | 151/154 |
| 34 | R-04 | on time | 0 | 0.0 | 5.1 | 0 | 140/147 |
| 35 | R-23 | on time | 0 | 0.0 | 5.0 | 0 | 128/133 |
| 36 | R-16 | on time | 0 | 0.0 | 4.0 | 0 | 145/147 |
| 37 | R-30 | on time | 0 | -1.0 | 5.0 | 0 | 147/147 |
| 38 | R-13 | on time | 0 | -1.0 | 4.8 | 0 | 133/133 |
| 39 | R-29 | on time | 0 | -1.0 | 4.0 | 0 | 148/154 |
| 40 | R-06 | on time | 0 | -1.5 | 3.7 | 0 | 114/119 |

## Directive (a)

No tool output, trace or this document contains rider phone numbers or complaint text. `trips.csv` has none (0 phone-like matches). The ops log is only ever reported as line number + day + category (`ops_log_summary`). It has 1 complaint-category line (line 5) and 0 phone-like strings.

## How to re-run (the tool)

```
pip install mcp pandas
python server/ontime_server.py --selftest      # smoke test
# Claude Code picks it up from .mcp.json as `ontime`. Tools:
#   data_quality, route_verdicts, route_detail, sensitivity, cost, ops_log_summary
# Every threshold is a parameter; data_path / schedule_path point at a new week's files.
```
