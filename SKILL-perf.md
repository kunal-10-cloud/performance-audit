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
  arguments={"command": "curl -sL https://raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_run_audit.py -o /tmp/perf_run_audit.py && python3 /tmp/perf_run_audit.py /app", "timeout": 120}
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

### Step 3b — Lighthouse Core Web Vitals (Desktop + Mobile)

**Skip if:** template from Step 3 is `expo` (native app, no web preview). Note "Lighthouse: N/A (native app)" and proceed to Step 4.

**Lighthouse MCP URL:** `https://lighthousemcp-566766422032.us-central1.run.app`

Where `{preview_url}` defaults to `https://{slug}.preview.emergentagent.com` unless the user provided a different URL.

**Infrastructure note:** The Lighthouse MCP runs on Cloud Run with **concurrency=1** per container. This means desktop and mobile audits each get their own fresh container instance (no shared memory state, no Chrome contamination between audits). Don't try to reuse session IDs between desktop and mobile — they're on different containers.

---

#### Phase 1: Warm-up (before any Lighthouse call)

**1a. Warm the Lighthouse MCP container:**

```bash
curl -s https://lighthousemcp-566766422032.us-central1.run.app/health
```

If it returns `{"status":"ok",...}` — proceed.
If it returns 503 or times out — wait 5 seconds and retry up to 3 times.
If still failing after 3 retries — skip Lighthouse entirely, note "Lighthouse MCP unavailable" in report, proceed to Step 4.

**1b. Warm the preview URL:**

```bash
curl -s -o /dev/null "{preview_url}"
```

This ensures the app's dev server has compiled assets and is ready. Without this, Lighthouse measures a cold-starting app and scores are artificially worse.

---

#### Phase 2: Desktop Audit (with retries)

Follow this exact MCP protocol flow. **Retry up to 3 times** if any step fails.

**Step 1 — Initialize MCP session:**
```bash
curl -s -D /tmp/lh_headers -o /tmp/lh_body -X POST https://lighthousemcp-566766422032.us-central1.run.app/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"perf-audit","version":"1.0.0"}}}'
```

Extract the session ID from the response headers:
```bash
SESSION_ID=$(grep -i "mcp-session-id" /tmp/lh_headers | sed 's/.*: //' | tr -d '\r\n')
```

If `SESSION_ID` is empty or the response was 503 — this is a cold start failure. Wait 5 seconds, hit `/health` again, and retry from Step 1.

**Step 2 — Call the desktop audit:**
```bash
curl -s --max-time 300 -X POST https://lighthousemcp-566766422032.us-central1.run.app/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: {SESSION_ID}" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_lighthouse_audit","arguments":{"url":"{preview_url}","categories":["performance"],"deviceType":"desktop"}}}'
```

**Success:** Response contains `"result":{"content":[{"type":"text","text":"..."}]}` with JSON scores and metrics.

**Failure indicators (retry if any):**
- Empty response body
- `"error"` in response (e.g., "Server not initialized", "Bad Request")
- HTTP 503 or 504
- Timeout (no response within 300 seconds)

**Retry procedure:**
```
Attempt 1: Run Steps 1-2 as above
  If fails → wait 5 seconds
Attempt 2: Hit /health first to re-warm, then fresh Steps 1-2
  If fails → wait 5 seconds
Attempt 3: Hit /health, fresh Steps 1-2
  If fails → record "Desktop Lighthouse: unavailable after 3 attempts — {last error}"
             Continue to mobile audit (it may work on a fresh container)
```

---

#### Phase 3: Mobile Audit (fresh session, with retries)

**Important:** Cloud Run is configured with `concurrency=1`, so each audit request lands on a **separate container instance**. The desktop session ID is NOT reusable for mobile — they're on different containers. Always create a fresh session for mobile.

**Step 1 — Initialize a fresh MCP session:**
```bash
curl -s -D /tmp/lh_hm -o /tmp/lh_bm -X POST https://lighthousemcp-566766422032.us-central1.run.app/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"perf-audit-mobile","version":"1.0.0"}}}'
```

Extract the session ID:
```bash
MOBILE_SESSION_ID=$(grep -i "mcp-session-id" /tmp/lh_hm | sed 's/.*: //' | tr -d '\r\n')
```

If `MOBILE_SESSION_ID` is empty or 503 — cold start failure. Wait 5s, hit `/health`, retry.

**Step 2 — Call the mobile audit:**
```bash
curl -s --max-time 300 -X POST https://lighthousemcp-566766422032.us-central1.run.app/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: {MOBILE_SESSION_ID}" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_lighthouse_audit","arguments":{"url":"{preview_url}","categories":["performance"],"deviceType":"mobile"}}}'
```

**Retry procedure (identical to desktop):**
```
Attempt 1: Steps 1-2 as above
  If fails (empty body, 503, "Server not initialized", timeout) → wait 5 seconds
Attempt 2: Hit /health, fresh Steps 1-2
  If fails → wait 5 seconds
Attempt 3: Hit /health, fresh Steps 1-2
  If fails → record "Mobile Lighthouse: unavailable after 3 attempts — {last error}"
             Continue to Step 4
```

---

#### Output

**Desktop output includes:**
- **Performance score** (0-100)
- **Core Web Vitals:** FCP, LCP, CLS, TBT, Speed Index, TTI
- **Opportunities:** Ranked improvements with estimated time savings in ms
- **Diagnostics:** LCP element, layout shifts, long tasks, DOM size, unused JS/CSS

**Mobile output additionally includes:**
- **Mobile-specific diagnostics:** `tap-targets` (touch target sizes), `viewport` (meta viewport check), `font-display` (font loading strategy)
- **Throttled metrics:** Moto G Power emulation (412x823@1.75), 4G throttling (150ms latency, 1638kbps download, 4x CPU slowdown)

**Threshold ratings** (Google's published standards):

| Metric | Good | Needs Improvement | Poor |
|--------|------|-------------------|------|
| LCP | <=2500 ms | 2500-4000 ms | >4000 ms |
| FCP | <=1800 ms | 1800-3000 ms | >3000 ms |
| CLS | <=0.1 | 0.1-0.25 | >0.25 |
| TBT | <=200 ms | 200-600 ms | >600 ms |

---

#### Graceful degradation

| Scenario | What to do in report |
|---|---|
| Desktop succeeded, mobile failed | Show desktop metrics normally. Mobile column shows "N/A — unavailable". Skip Mobile vs Desktop Comparison. |
| Desktop failed, mobile succeeded | Show mobile metrics normally. Desktop column shows "N/A — unavailable". |
| Both failed | Omit Lighthouse Metrics Dashboard and Mobile Responsiveness sections entirely. Note "Lighthouse MCP was unavailable — report based on static analysis only." Static analysis findings are always available. |
| Both succeeded | Full report with desktop + mobile comparison. |

**IMPORTANT:** Lighthouse failures must NEVER block the pipeline. Static analysis (Step 3) and runtime evidence (Step 4) are independent and always produce results regardless of Lighthouse status.

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

**Static <-> Desktop Lighthouse correlation:**
- Poor LCP + static "missing code splitting" or "heavy bundle" -> mark as "Confirmed by Lighthouse"
- Poor TBT + static "sync route handlers" or "blocking work" -> mark as confirmed
- Poor CLS + static "no image dimensions" -> mark as confirmed
- Lighthouse score 90+ -> note "Runtime metrics healthy" — static findings are optimization opportunities
- Lighthouse opportunities with savings >100ms -> map to fix prompts

**Desktop <-> Mobile Lighthouse comparison:**
- Mobile score 20+ points below desktop -> flag as "Significant mobile performance gap" in the report
- Mobile LCP > 4000ms while desktop LCP < 2500ms -> correlate with static "missing responsive images" or "no code splitting"
- Mobile TBT > 600ms -> correlate with static "heavy bundle" or "unminified JS"
- `tap-targets` diagnostic failing -> correlate with static "small touch targets" finding
- `viewport` diagnostic failing -> correlate with static "missing meta viewport" finding
- If mobile audit unavailable -> skip comparison, note in report

**Static mobile checks <-> Mobile Lighthouse correlation:**
- Static "missing meta viewport" + Lighthouse `viewport` failing -> mark as "Confirmed by Lighthouse"
- Static "no media queries" + poor mobile score -> mark as "Confirmed — no responsive layout"
- Static "100vh usage" + mobile CLS > 0.1 -> mark as confirmed

**Static <-> Runtime correlation:**
- Runtime timeout errors -> confirm slow endpoint findings
- Runtime memory warnings -> confirm memory leak findings
- No runtime evidence -> present static findings as "predicted" (still valid)

---

### Step 6 — Compile Report

Write the final report as markdown at `perf-audit/reports/{slug}.md`. Follow this exact template structure. Every report MUST use this format.

#### Report Template

```markdown
# Performance Audit Report — `{slug}`

**Verdict:** {emoji} **{verdict}** · **Overall Score:** {lighthouse_score} / 100
**Template (detected):** `{template}` ({human-readable description})
**Job ID:** `{job_id}`
**Preview URL:** {preview_url}

---

## Executive Summary

### Severity counts
{Table: Priority | Count | When to fix}
- CRITICAL = Fix before launch — will cause real user-facing issues
- HIGH = Fix as you scale — problems appear with more users/data
- LOW = Nice to have — minor optimizations

### Per-category breakdown
{Table: Category | Critical | High | Low | Pass | Total Checks | Score}
{Categories: Backend Performance, Rendering Performance, Database, Algorithms & Code Quality, Mobile & Responsive}
{Mobile & Responsive row omitted for Expo apps}

### Top 3 highest-impact findings
{Numbered list — 1-sentence plain-language summary each}

### Lighthouse Metrics Dashboard
{Table: Metric | Desktop | Mobile | Target | Status emoji}
{Show both desktop and mobile values side by side. If mobile audit failed, show "N/A" with a note.}
{Below the table, a "What these mean" block explaining each metric in plain language}

---

## Technical Findings

### CRITICAL findings — Fix before launch
{For each finding:}
#### **CRITICAL** · PERF-{NNN} · {Category} · {Title}
- **Location:** {file:line references}
- **Evidence:** {Technical evidence — what the check found}
- **Impact:** {Technical impact}
> **In plain terms:** {1-2 sentence analogy a non-developer can understand}
- **Fix prompt:** {Copy-pasteable prompt for the agent}
- **After fixing:** {What improves, in plain language}

### HIGH findings — Fix as you scale
{Same format as CRITICAL}

### LOW findings — Nice to have
{Same format, but "In plain terms" block optional for trivial findings}

### PASS — What's working well
{Table: Check | Status | Notes — one row per passing check}

---

## Lighthouse Opportunities
{Table: Opportunity | Estimated Savings | What it means — plain language}

---

## Mobile Responsiveness

### Mobile vs Desktop Comparison
{Table: Metric | Desktop | Mobile | Delta | Status}
{Include: Performance Score, LCP, FCP, CLS, TBT}
{Delta column shows the difference. Flag as 🔴 if mobile is 20+ points worse or metric crosses a threshold boundary}
{If mobile audit unavailable: show "N/A" for mobile column with note: "Mobile Lighthouse audit failed: {error}"}

### Mobile-Specific Diagnostics (from Lighthouse)
{Table: Diagnostic | Result | What it means}
{Include: tap-targets, viewport, font-display — only if mobile audit ran}
{If mobile audit failed: "Mobile Lighthouse audit was unavailable. Mobile findings below are from static analysis only."}

### Mobile Static Analysis Findings
{List PERF-IDs from the mobile_responsive category in the standard finding format}
{These always appear regardless of whether mobile Lighthouse ran — they come from perf_audit.py}

---

## Runtime Evidence
{Table: Signal | Result}

---

## Remediation Roadmap

### Fix before launch (Critical)
{Table: # | Finding | What to do | Expected improvement}

### Fix as you scale (High)
{Same table format}

### Nice to have (Low)
{Same table format}

---

## Pipeline Steps Executed
{Table: Step | Name | Source | Status}

---

## Summary
{Table: Metric | Value — key stats}

---
*Report generated by `perf-audit` pipeline · {date}*
```

#### Verdict rules

| Lighthouse Score | Verdict |
|---|---|
| >= 90 | 🟢 **EXCELLENT** |
| 75-89 | 🟢 **GOOD** |
| 50-74 | 🟡 **NEEDS WORK** |
| < 50 | 🔴 **POOR** |

#### Severity assignment rules

Assign each failing check a priority based on user impact:

| Priority | Criteria |
|---|---|
| **CRITICAL** | Will cause visible user-facing problems at current scale — crashes, freezes, broken flows, major load time issues confirmed by Lighthouse |
| **HIGH** | Works now but will degrade with more users/data — scaling issues, accumulating inefficiencies |
| **LOW** | Minor optimizations with marginal impact — cosmetic jank, small CPU overhead |

#### Finding format rules

- Every finding gets a PERF-{NNN} ID (sequential)
- Technical location, evidence, and impact are required — keep full technical accuracy
- **"In plain terms"** block is required for every CRITICAL and HIGH finding — use a real-world analogy
- **"After fixing"** line is required for every finding — describe the user-visible improvement
- **Fix prompt** must be self-contained and copy-pasteable into the Emergent agent
- PASS checks go in a summary table — one row per check, with a brief "Notes" column explaining what it means
- Lighthouse opportunities table must include a plain-language "What it means" column
- Remediation roadmap groups findings by priority with "Expected improvement" column

#### Report style rules

- Keep all technical details intact — file paths, line numbers, evidence, check names
- Add plain-language context below/beside technical content, never instead of it
- Use status emojis in tables (🟢 🟡 🔴) for visual scanning
- No jargon without explanation — if you use a technical term (LCP, TBT, N+1, etc.), add a brief note on what it means
- Lead with what's working (PASS checks) before diving into failures where appropriate
- The report should be readable by both a developer (who skips the "In plain terms" blocks) and a non-technical founder (who reads them)

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
