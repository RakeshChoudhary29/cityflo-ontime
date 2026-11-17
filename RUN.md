# Running the `ontime` MCP server

Python 3.10+.

```
pip install mcp pandas
```

Put the take-home bundle in `data/`: `trips.csv`, `schedule_v2.csv`, `ops_log.txt` from https://careers.cityflo.com/takehomes/ai-engineer/.

- Smoke test: `python server/ontime_server.py --selftest`
- Claude Code: `.mcp.json` registers it as `ontime` (stdio). Start `claude` in this folder and approve the server.
- Any other MCP client: command `python server/ontime_server.py`.

| Tool | What it answers |
|---|---|
| `data_quality` | Which rows each cleaning rule dropped or fixed (counts + trip ids), plus the REC-114 match report |
| `route_verdicts` | One verdict per route, ranked, with worst three and borderline routes. Every threshold is a parameter. |
| `route_detail` | Drill-down for one route: by day, by hour, latest trips, excluded trips |
| `sensitivity` | Late set and top three under alternative rules (thresholds, ranking, cleaning, REC-114) |
| `cost` | Depot incentives lost vs refund-level trips on flagged and cleared routes |
| `ops_log_summary` | Ops-log lines as day/time/category only. No text, per directive (a). |

For a new week, pass `data_path` / `schedule_path`. For a new constraint, pass `hour_from`/`hour_to`, `exclude_days`, `late_threshold_min`, `min_late_share`, `refund_threshold_min` or `rec114_mode`.

The decisions are in `DECISION.md`; the steering ledger is in `decisions.jsonl`.
