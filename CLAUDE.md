# Performance Audit Pipeline

Automated performance audit for Emergent apps. Runs static analysis on the pod and measures Core Web Vitals via Lighthouse (desktop + mobile), then produces a report with copy-pasteable fix prompts.

## Quick Start

```
Run performance audit for job <job_id> on slug <slug_name>
Run performance audit for job <job_id> on slug <slug_name> with preview url <url>
```

## Setup (one-time)

### 1. Environment Variable

Set your Emergent admin JWT token:

```bash
export EMERGENT_AUTH_TOKEN="your-jwt-token"
```

Get it from: browser DevTools -> Network tab -> any `api.emergent.sh` request -> copy the `Authorization` header value (without `Bearer ` prefix).

This token expires periodically. If you get 401 errors, refresh it.

Operators need **admin access to the Emergent platform** for their token to have permission to wake customer pods.

### 2. MCP Servers

Add these to your `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "e1": {
      "type": "http",
      "url": "https://your-e1-gateway-url/mcp"
    },
    "lighthouse-mcp": {
      "type": "http",
      "url": "https://lighthousemcp-566766422032.us-central1.run.app/mcp"
    }
  }
}
```

**e1** — Emergent pod execution gateway. Required to run the audit script on pods.

**lighthouse-mcp** — Measures Core Web Vitals. Already hosted on GCP Cloud Run at the URL above (concurrency=1, 2GB memory per container).

## File Structure

```
perf-audit/
  CLAUDE.md              <- This file (setup docs)
  SKILL-perf.md          <- Skill definition (the pipeline steps)
  perf_audit.py          <- Static analysis engine (runs on pod)
  perf_run_audit.py      <- Runner wrapper (downloads + formats output)
  process_perf_csv.py    <- Batch orchestrator for CSV-driven audits
  reports/               <- Generated audit reports
  .claude/
    settings.json        <- Pre-approved tool permissions
```

## Pipeline Flow

```
User: "Run performance audit for job X on slug Y"
  |
  v
Step 1: Check pod status (awake/sleeping)
Step 2: Wake pod if sleeping (wait 15s)
  |
  v
Step 3: Static analysis via e1 MCP (runs perf_audit.py on pod)
  |  -> 36 checks: backend, rendering, database, algorithms, mobile
  |  -> PASS/FAIL report + fix prompts
  |
Step 3b: Lighthouse audit via lighthouse-mcp (desktop + mobile)
  |  -> Core Web Vitals: LCP, FCP, CLS, TBT
  |  -> Opportunities ranked by time savings
  |  (skipped for Expo/native apps)
  |
Step 4: Correlate static + Lighthouse findings
Step 5: Compile report -> reports/{slug}.md
Step 6: Report completion summary
```

## Batch Mode (CSV)

For auditing multiple apps from a CSV:

```bash
# Set auth token
export EMERGENT_AUTH_TOKEN="your-token"

# Phase 1: Check pods, wake sleeping ones, build queue
python3 process_perf_csv.py prep input.csv 0 50

# Phase 2: Write results (called per slug after audit)
python3 process_perf_csv.py write input.csv slug-name check_report.txt fix_prompt.txt

# Phase 3: Mark skipped
python3 process_perf_csv.py skip input.csv slug-name "reason"
```

CSV must have columns: `slug`, `latest_job_id`, `Perf eval`, `Fix prompt`.

## Updating the Audit Scripts

The static analysis scripts are served from this GitHub repo via raw URLs. Pods download them at runtime:

- `perf_run_audit.py` — downloaded by SKILL Step 3 from `raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_run_audit.py`
- `perf_audit.py` — downloaded by `perf_run_audit.py` from `raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_audit.py`

To update: just edit the files and push to `main`. Every subsequent audit will use the latest version automatically.

## For xtools Integration

When pushing to xtools, copy ONLY `SKILL-perf.md`. Do not copy:
- `perf_audit.py` / `perf_run_audit.py` — already hosted on this GitHub repo's raw URLs, skill references them directly
- `CLAUDE.md` — local setup docs, not needed in xtools
- `reports/` — output folder
- `process_perf_csv.py` — local batch tooling
- `.claude/` — Claude Code config
