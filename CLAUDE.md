# Performance Audit Pipeline

Automated performance audit for Emergent apps. Runs static analysis on the pod, measures Core Web Vitals via Lighthouse, collects runtime evidence, and drafts user-facing emails with fix prompts.

## Quick Start

```
Run performance audit for job <job_id> on slug <slug_name>
Run performance audit for job <job_id> on slug <slug_name> with preview url <url>
```

## Setup (one-time)

### 1. Environment Variable

Set your Emergent auth token:

```bash
export EMERGENT_AUTH_TOKEN="your-jwt-token"
```

Get it from: browser DevTools -> Network tab -> any `api.emergent.sh` request -> copy the `Authorization` header value (without `Bearer ` prefix).

This token expires periodically. If you get 401 errors, refresh it.

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

**lighthouse-mcp** — Measures Core Web Vitals. Already hosted on GCP Cloud Run at the URL above.

**cortex_debugger** — Internal Emergent MCP for runtime logs. If unavailable, the pipeline runs with static analysis only.

**Gmail** — Claude Code's built-in Gmail MCP. Used to draft emails. Optional — the pipeline works without it, you just won't get Gmail drafts.

## File Structure

```
perf-audit/
  CLAUDE.md              <- This file
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
  |  -> 22+ checks: backend, rendering, database, algorithms
  |  -> PASS/FAIL report + fix prompts
  |
Step 3b: Lighthouse audit via lighthouse-mcp
  |  -> Core Web Vitals: LCP, FCP, CLS, TBT
  |  -> Opportunities ranked by time savings
  |  (skipped for Expo/native apps)
  |
Step 4: Runtime evidence via cortex debugger (optional)
  |  -> Timeout errors, memory warnings, build failures
  |
Step 5: Correlate static + Lighthouse + runtime findings
Step 6: Compile report -> reports/{slug}.md
Step 7: Draft Gmail email with top 3 fix prompts
Step 8: Report completion
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
