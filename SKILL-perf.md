---
name: perf-audit
description: Performance audit pipeline (8 steps). Takes a Job ID + Slug Name + Preview URL, wakes sleeping pods, runs static performance analysis, measures Core Web Vitals via Lighthouse MCP, collects runtime evidence via cortex debugger, correlates findings, generates fix prompts, and drafts user email.
---

# Performance Audit Skill

## Purpose
Run a pre-launch performance audit on an Emergent app. Detects the app template (Next.js / Expo / Farm), runs 22+ static checks across backend, rendering, database, and algorithms, measures Core Web Vitals via Google Lighthouse, correlates static and runtime findings, then produces a PASS/FAIL report with copy-pasteable fix prompts.

---

## How to Run

Provide a **Job ID**, **Slug Name**, and optionally a **Preview URL**:

```
Run performance audit for job <job_id> on slug <slug_name>
Run performance audit for job <job_id> on slug <slug_name> with preview url <url>
```

If no preview URL is given, the default is `https://{slug}.preview.emergentagent.com`.

---

## Required MCPs

| MCP | Purpose | Setup |
|---|---|---|
| **e1** | Run perf analysis script on the pod via `env_key` | Must be configured in `~/.claude.json` — see CLAUDE.md |
| **cortex_debugger** | Fetch runtime logs for performance evidence | Internal Emergent MCP |
| **lighthouse-mcp** | Measure Core Web Vitals (LCP, FCP, CLS, TBT) | Deploy via `lighthouse-mcp/` or use hosted instance |
| **Gmail** | Draft user email via `mcp__claude_ai_Gmail__create_draft` | Claude Code Gmail MCP |

---

## Environment Variables

The pipeline uses `{AUTH_TOKEN}` in API calls. This must be set as `EMERGENT_AUTH_TOKEN`:

```bash
export EMERGENT_AUTH_TOKEN="your-jwt-token-here"
```

Get it from: browser DevTools -> Network tab -> any `api.emergent.sh` request -> `Authorization` header value (without the `Bearer ` prefix).

---

## Pipeline (8 Steps)

### Step 1 — Check Pod Status

```
GET https://api.emergent.sh/trajectories/v0/stream?job_id={job_id}&last_request_id=5b6feb4e-b686-4e22-82f5-87aeee44fb32
```

Headers: `accept: text/event-stream`, `authorization: Bearer {AUTH_TOKEN}`, `cache-control: no-cache`, `content-type: application/json`, `origin: https://app.emergent.sh`

Where `{AUTH_TOKEN}` is the value of the `EMERGENT_AUTH_TOKEN` environment variable.

- Response has data -> **awake** (skip to Step 3)
- Response empty -> **sleeping** (proceed to Step 2)

---

### Step 2 — Wake Up Pod

```
POST https://api.emergent.sh/jobs/v0/{job_id}/restart-environment?upgrade=false&source=manual_wakeup
```

Same headers. Retry up to 3 times with 2s delay. Wait 15s after success for boot.

---

### Step 3 — Static Performance Analysis

Run on the pod via e1 MCP:

```
mcp__e1__execute_tool(
  env_key="{slug}",
  tool_name="execute_bash",
  arguments={"command": "curl -sL https://files.catbox.moe/mhkhlc.py -o /tmp/perf_run_audit.py && python3 /tmp/perf_run_audit.py /app", "timeout": 120}
)
```

> **Architecture:** `perf_run_audit.py` (wrapper) downloads `perf_audit.py` (engine) from its hardcoded URL, runs it, parses JSON, and formats the PASS/FAIL report. Two files on catbox — only the wrapper URL goes here.

The pipeline automatically:
1. **Detects template** from `package.json` / `app.json` / `next.config.js`
2. **Runs backend checks** (9 checks — async handlers, N+1, unbounded queries, mongo singleton, indexes, sequential async, blocking handlers, over-fetching, pydantic overhead)
3. **Runs template-specific checks** (Next.js: 10, Expo: 8, Farm: 5)
4. **Runs cross-cutting checks** (algorithmic complexity, data structures, promise parallelization)

**Output:** Two sections separated by `===SEPARATOR===`

- **Section 1 — Check Results:** Each check as PASS or FAIL with file:line locations for failures. Summary table with pass/fail counts per category.
- **Section 2 — Fix Prompts:** One self-contained prompt per failing check type. Copy-pasteable.

---

### Step 3b — Lighthouse Core Web Vitals

**Skip if:** template from Step 3 is `expo` (native app, no web preview). Note "Lighthouse: N/A (native app)" and proceed to Step 4.

Call the Lighthouse MCP tool to measure real browser performance:

```
run_lighthouse_audit(
  url: "{preview_url}",
  categories: ["performance"]
)
```

Where `{preview_url}` defaults to `https://{slug}.preview.emergentagent.com` unless the user provided a different URL.

**Output includes:**
- **Performance score** (0-100)
- **Core Web Vitals:** FCP, LCP, CLS, TBT, Speed Index, TTI
- **Opportunities:** Ranked improvements with estimated time savings in ms
- **Diagnostics:** LCP element, layout shifts, long tasks, DOM size, unused JS/CSS

**Threshold ratings** (Google's published standards):

| Metric | Good | Needs Improvement | Poor |
|--------|------|-------------------|------|
| LCP | <=2500 ms | 2500-4000 ms | >4000 ms |
| FCP | <=1800 ms | 1800-3000 ms | >3000 ms |
| CLS | <=0.1 | 0.1-0.25 | >0.25 |
| TBT | <=200 ms | 200-600 ms | >600 ms |

**Note:** Measured after cold start (pod was just woken in Step 2). Metrics may be slightly inflated vs warm requests. Note "measured after cold start" in the report.

**Error handling:**
- Template is `expo` -> skip entirely, note "N/A (native app)"
- Connection refused / timeout -> record failure, continue to Step 4
- Lighthouse error -> record error message, continue to Step 4

---

### Step 4 — Runtime Evidence (Optional)

Use cortex debugger to look for runtime performance signals:

1. **Fetch job logs:**
   ```
   get_job_logs(job_id="{job_id}", pattern="error|timeout|slow|memory|OOM")
   ```

2. **Fetch cost summary:**
   ```
   get_job_cost_summary(job_id="{job_id}")
   ```

If runtime evidence confirms a static finding, mark it as confirmed. If logs are unavailable (pod was sleeping), skip — static analysis findings are still valid.

---

### Step 5 — Correlate & Confirm Findings

Cross-reference Step 3 static findings with Step 3b Lighthouse results and Step 4 runtime evidence:

**Static <-> Lighthouse correlation:**
- Poor LCP + static "missing code splitting" or "heavy bundle" -> mark as "Confirmed by Lighthouse"
- Poor TBT + static "sync route handlers" or "blocking work" -> mark as confirmed
- Poor CLS + static "no image dimensions" -> mark as confirmed
- Lighthouse score 90+ -> note "Runtime metrics healthy" — static findings are optimization opportunities
- Lighthouse opportunities with savings >100ms -> map to fix prompts

**Static <-> Runtime correlation:**
- Runtime timeout errors -> confirm slow endpoint findings
- Runtime memory warnings -> confirm memory leak findings
- No runtime evidence -> present static findings as "predicted" (still valid)

---

### Step 6 — Compile Report

Write the final report as markdown at `perf-audit/reports/{slug}.md`:

```
## Performance Audit Report: {slug}

**Job ID / Slug / Template / Date**

### 1. Backend Performance Checks
{Each check as PASS or FAIL with inline locations}

### 2. Rendering Performance Checks ({Template})
{Template-specific checks}

### 3. Database Checks
{Same format}

### 4. Algorithm & Code Quality Checks
{Same format}

### 5. Lighthouse Core Web Vitals
- Performance Score: {X}/100
- LCP: {value} ({Good/Needs Improvement/Poor})
- FCP: {value} ({rating})
- CLS: {value} ({rating})
- TBT: {value} ({rating})
- Top Opportunities: {list with savings}
(Or: "Skipped — Expo native app" / "Failed — {reason}")

### 6. Fix Prompts
{One prompt per failing check — self-contained, copy-pasteable}

### Summary
| Check Category | Pass | Fail | Total |
{Per-category pass/fail counts}
| Lighthouse Score | {X}/100 |
```

**Report rules:**
- Do NOT show scores, grades, severity levels, or scoring formulas from static analysis
- DO show every check as PASS or FAIL
- Failing checks MUST include file:line locations
- Fix prompts grouped by check type, not by individual finding
- DO show Lighthouse metrics with threshold ratings

---

### Step 7 — Draft Gmail Email

Create a Gmail draft with the top 3 fix prompts:

```
mcp__claude_ai_Gmail__create_draft(
  to: ["{customer_email}"],
  subject: "Performance check for your Emergent app",
  htmlBody: "{HTML email}"
)
```

**Email template:** Table-based HTML with inline CSS. Sections:
1. **Header:** "Performance check for your app"
2. **Greeting:** "Hi there" (never user's name)
3. **Performance score section:** If Lighthouse ran, show score and key metrics (LCP, FCP, CLS, TBT) in a table. Keep it plain language.
4. **Health check section:** 2-3 sentences, positive first, then top issues. Do NOT mention internal scores/grades.
5. **Top 3 fix prompts** in separate code block cards (labeled Prompt 1, 2, 3)
6. **Calendly CTA:** `https://calendly.com/d/ct45-y7p-23p/emergent-consultation?from=slack`
7. **Footer:** "Best, Emergent Team" (never individual name)

**HTML rules:**
- Table-based layout (not divs) for Gmail/Outlook compatibility
- ALL CSS inline
- Use HTML entities for emojis
- `contentType: text/html`

---

### Step 8 — Report Completion

Report the draft ID and confirm it's ready for review. Present the summary table to the operator.

---

## Error Handling

- Pod wake fails -> report error, skip to Step 4 if possible
- Performance analysis fails -> note in report, attempt runtime evidence only
- Lighthouse fails -> skip Step 3b, continue with static-only analysis
- Template is `expo` -> skip Lighthouse, note "N/A (native app)"
- Cortex debugger unreachable -> skip Step 4, use static analysis only
- Template not detected -> run as "generic" (all template checks)

## Constraints

- Only report what is directly observable in the codebase or measured by Lighthouse
- If a file cannot be read, note as unverified — don't assume pass or fail
- Do not recommend architectural changes beyond pre-launch scope
