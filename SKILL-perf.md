---
name: perf-audit
description: Performance audit pipeline. Takes a Job ID + Slug Name + Preview URL, wakes sleeping pods, runs static performance analysis, measures Core Web Vitals via Lighthouse MCP (desktop + mobile), correlates findings, and generates a professional report with fix guidance for each issue.
---

# Performance Audit Skill

## Purpose
Run a pre-launch performance audit on an Emergent app. Detects the app template (Next.js / Expo / Farm), runs 36 static checks across backend, rendering, database, algorithms, and mobile responsiveness, measures Core Web Vitals via Google Lighthouse on desktop and mobile, correlates static findings with Lighthouse results, then produces a report that describes each issue and offers fix guidance. **The report provides approach guidance and patterns to adapt — not ready-to-paste prompts. The final prompt wording is up to the operator.**

---

## How to Run

Provide a **Job ID**, **Slug Name**, and optionally a **Preview URL**:

```
Run performance audit for job <job_id> on slug <slug_name>
Run performance audit for job <job_id> on slug <slug_name> with preview url <url>
```

If no preview URL is given, the default is `https://{slug}.preview.emergentagent.com`.

---

## Prerequisites

### Required MCPs

| MCP | Purpose | Setup |
|---|---|---|
| **e1** | Run perf analysis script on the pod via `env_key` | Emergent platform MCP — already wired into Overwatch/Claude Code |
| **lighthouse-mcp** | Measure Core Web Vitals (LCP, FCP, CLS, TBT) | Hosted at `https://lighthousemcp-566766422032.us-central1.run.app` — no local setup needed |

### Hosted scripts

The static analysis scripts (`perf_audit.py`, `perf_run_audit.py`) are hosted on GitHub raw URLs and downloaded onto the pod at audit time:
- `https://raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_run_audit.py`
- `https://raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_audit.py`

The skill references these URLs directly — no need to bundle the scripts with this skill.

---

## Authentication

This skill makes authenticated calls to `api.emergent.sh` (for waking pods and checking status). It uses the **operator's own Emergent admin JWT token** — NOT a shared service account.

**Requirement:** The operator running this audit must have **admin access to the Emergent platform** so their token has permission to wake customer pods.

### How the token is resolved

The skill uses a placeholder `{AUTH_TOKEN}` in API calls. At runtime, resolve it in this order:

1. **If `EMERGENT_AUTH_TOKEN` environment variable is set** — use that value directly.
2. **Otherwise** — prompt the operator once at the start of the audit:
   > "Please paste your Emergent admin JWT token. You can get it from: browser DevTools → Network tab → any request to `api.emergent.sh` → copy the `Authorization` header value (without the `Bearer ` prefix). Or from browser console: `copy(JSON.parse(localStorage.getItem('sb-snksxwkyumhdykyrhhch-auth-token')).access_token)`"

Store the token in memory for the duration of the audit session only. **Never log it, write it to a report, or include it in any committed file.**

### Security rules

- Never hardcode a token in this skill or any file derived from it
- Never paste an actual token into reports, emails, or logs
- The token is scoped to the individual operator — each operator uses their own
- When this skill is invoked via Overwatch, Overwatch is expected to provide the token via `EMERGENT_AUTH_TOKEN` so the operator is not prompted manually

---

## Pipeline (6 Steps)

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

> **Architecture:** `perf_run_audit.py` (wrapper) is downloaded onto the pod first. It then downloads `perf_audit.py` (the analysis engine) from GitHub raw, runs it, parses the JSON output, and formats the PASS/FAIL report. Both files are hosted in `kunal-10-cloud/performance-audit` on GitHub — only the wrapper URL is referenced directly here.

The pipeline automatically:
1. **Detects template** from `package.json` / `app.json` / `next.config.js`
2. **Runs backend checks** (9 checks — async handlers, N+1, unbounded queries, mongo singleton, indexes, sequential async, blocking handlers, over-fetching, pydantic overhead)
3. **Runs template-specific checks** (Next.js: 10, Expo: 8, Farm: 5)
4. **Runs cross-cutting checks** (algorithmic complexity, data structures, promise parallelization)

**Output:** Two sections separated by `===SEPARATOR===`

- **Section 1 — Check Results:** Each check as PASS or FAIL with file:line locations for failures. Summary table with pass/fail counts per category.
- **Section 2 — Fix Guidance:** One fix approach per failing check type. These are **suggested approaches, not exact prompts** — the operator adapts them for their codebase.

---

### Step 3b — Lighthouse Core Web Vitals (Desktop + Mobile)

**Skip if:** template from Step 3 is `expo` (native app, no web preview). Note "Lighthouse: N/A (native app)" and proceed to Step 4 (Correlate).

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
If still failing after 3 retries — skip Lighthouse entirely, note "Lighthouse MCP unavailable" in report, proceed to Step 4 (Correlate).

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
| Desktop succeeded, mobile failed | Show desktop metrics normally. Mobile column shows "N/A — unavailable". Skip the Mobile vs Desktop Comparison table but keep Mobile Responsiveness section (static mobile findings are still valid). |
| Desktop failed, mobile succeeded | Show mobile metrics normally. Desktop column shows "N/A — unavailable". |
| Both failed | Omit the Lighthouse Metrics Dashboard and the Lighthouse-based parts of Mobile Responsiveness. Keep the "Mobile Static Analysis Findings" subsection — those come from `perf_audit.py` and are always available. Note "Lighthouse MCP was unavailable — report based on static analysis only." |
| Both succeeded | Full report with desktop + mobile comparison. |

**IMPORTANT:** Lighthouse failures must NEVER block the pipeline. Static analysis (Step 3) is independent and always produces results regardless of Lighthouse status.

---

### Step 4 — Correlate & Confirm Findings

Cross-reference Step 3 static findings with Step 3b Lighthouse results:

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

If Lighthouse was unavailable, present static findings as "predicted" — they're still valid, just not yet confirmed by browser measurement.

---

### Step 4b — Validate Findings (MANDATORY — do not skip)

Before compiling the report, validate every finding against reality. Lighthouse sometimes overstates problems due to simulated throttling, and the static analyzer can have false positives. **A report with inflated findings damages credibility far more than one with fewer, accurate findings.**

**Minimum validation required before any finding goes in the report as CRITICAL or HIGH:**

1. For every Lighthouse-based CRITICAL — run the corresponding `curl` validation from Rules 1-3 below
2. For every static-analysis CRITICAL — spot-check 2-3 file:line locations by reading the actual code via e1 MCP
3. Apply the noise filter (Rule 6) — drop findings below actionability threshold

Skipping this step means shipping an audit with known false positives. Don't.

#### Rule 1 — "Redirects" opportunity: verify main-document vs sub-resource redirects

If Lighthouse reports "Avoid multiple page redirects" with large savings (>500ms), do NOT assume the main URL has a redirect chain.

**Validation:**
```bash
curl -sL -o /dev/null -w "Redirects: %{num_redirects}\nFinal URL: %{url_effective}\n" {preview_url}
```

**Three possible outcomes:**

| curl result | Lighthouse savings | What's actually happening |
|---|---|---|
| 0 redirects | Small (<500 ms) | Lighthouse bug, ignore |
| 0 redirects | Large (>500 ms) | **Sub-resource redirect** — a 3rd-party script or image is redirecting. Find it by checking `<script src>` tags for `@latest` CDN versions, or `<img src>` for unpinned URLs. The fix is usually pinning versions, NOT infrastructure changes. |
| 1+ redirects on main URL | Any | Real main-page redirect chain. Report as infrastructure issue. |

**In the report:**
- If validation shows 0 main-page redirects, frame the finding as "sub-resource redirect" with the specific culprit identified
- Do NOT write prompts about nginx/Cloudflare/DNS config — those don't apply
- Lighthouse's savings number is often inflated by simulated throttling; note this in the finding

#### Rule 2 — "Oversized images" / "Properly size images": verify with actual image weights

If Lighthouse reports large KiB savings for image resizing, verify by checking actual image sizes:

**Validation:**
```bash
curl -sL {preview_url} | grep -oE 'src="[^"]+\.(jpg|jpeg|png|webp|gif)"' | head -10
# Then for each:
curl -sL -o /dev/null -w "%{size_download} bytes\n" {image_url}
```

If individual images are small (<100 KB each), the finding may be about many images adding up, not any single oversized one.

#### Rule 3 — "Unused JavaScript": verify with Coverage data

Lighthouse's "unused JS" number assumes only the code running on the CURRENT page counts as "used." It doesn't know about code needed for other routes.

**Validation:**
- For a multi-page app, some "unused" JS might actually be needed on OTHER pages (legitimately shipped in the main bundle)
- Check if the site is an SPA with many routes — if yes, some "unused" JS is expected

**In the report:**
- For SPAs: frame "unused JS" as "JS not needed on the current route" — code splitting is the fix
- For truly single-page sites: it's genuinely unused and should be removed

#### Rule 4 — Static analysis N+1 findings: spot-check for false positives

The static analyzer pattern-matches `.find()` calls inside loops, which can false-positive on:
- Loops that log events rather than query DB
- Loops that operate on in-memory collections, not DB
- Generator expressions that look like loops but aren't

**Validation:**
- Spot-check 3-5 of the N+1 findings by reading the actual code
- If 2+ are false positives, the total count is inflated — use "X+" phrasing rather than an exact number
- Example: "approximately 80+ N+1 patterns detected (some may be false positives; spot-checked 5, confirmed 4)"

#### Rule 5 — Static analysis unbounded queries: distinguish user-facing from utility

The analyzer flags ALL `.find()` without `.limit()`, but some are fine:
- Utility/migration scripts that run once — unbounded is correct
- Queries filtered to a single document (e.g., `find({"_id": id}).to_list()`) — not an issue
- Admin-only exports — less urgent than user-facing

**In the report:**
- Separate user-facing route handlers (CRITICAL) from utility scripts (LOW/informational)
- Don't lump `scripts/migrate_data.py` in with `routes/api.py`

#### Rule 6 — Noise filter: drop findings below actionability threshold

Lighthouse reports many tiny "opportunities" that aren't actually worth fixing. Drop or mark as informational-only:

| Type | Threshold | What to do |
|---|---|---|
| Lighthouse opportunity | < 200 ms estimated savings | Don't include as a finding — mention in "Lighthouse Opportunities" table only |
| Lighthouse diagnostic | < 10 KiB savings | Same — table only, not a finding |
| Static unbounded-query | Utility/migration script only | Mark as LOW or exclude |
| Static N+1 | Script that runs once a year | Mark as LOW or exclude |

**Rationale:** A "3 KiB JavaScript minification savings" finding is pure noise — fixing it gains 3 KB on a multi-MB page. Including trivial findings dilutes the serious ones.

#### Rule 7 — Framework mismatch check

The `perf_audit.py` static analyzer runs all three template check suites (Next.js, Expo, Farm) for "generic" projects. This can produce inappropriate recommendations on React Create-React-App (CRA), Vite, or other non-matching frameworks.

**Before including framework-specific findings, verify the framework is actually used:**

```bash
# Run on the pod to detect framework
cat /app/frontend/package.json | grep -E '"(next|expo|@farmfe|vite|react-scripts)"'
```

**Framework-to-finding mapping:**

| Framework detected | DROP these findings if they appear |
|---|---|
| Create React App (`react-scripts`) | "Next.js Image", "next dev", "getServerSideProps", "Hermes engine", "jsEngine: hermes" |
| Vite | Same as CRA above |
| Next.js | "Hermes engine" |
| Expo / React Native | "Next.js Image", "next dev", "getServerSideProps" |

**If dropped:** replace with framework-appropriate guidance. For example:
- On CRA + native `<img>`: recommend `loading="lazy"` + `srcset` (not `next/image`)
- On CRA + getServerSideProps: not applicable (drop the finding entirely)

#### What to do when validation fails

If you find Lighthouse or static analysis misrepresenting a finding:

1. **Still include the finding if real** — but with corrected framing
2. **Note the discrepancy explicitly** — "Lighthouse reported X ms savings; `curl` verification shows Y — the effective savings is likely Z"
3. **Correct the severity** — if it's not as bad as Lighthouse says, downgrade from CRITICAL to HIGH or LOW
4. **Give an accurate fix** — don't suggest infrastructure changes when the fix is a 1-line HTML edit
5. **Drop entirely** if below noise threshold (Rule 6) or framework-inappropriate (Rule 7)

**If >30% of findings need correction after validation,** note in the report: "Static analyzer had notable false-positive rate on this audit; counts reflect hand-validated subset only."

---

### Step 5 — Compile Report

Write the final report as markdown at `perf-audit/reports/{slug}.md`. Follow this exact template structure. Every report MUST use this format.

#### CRITICAL RULE: Never reuse citations from previous reports

Every `file:line` reference, function name, module name, and endpoint URL in the report MUST come from the **current audit's output** — not from memory, not from a previous audit of a different codebase, and not copy-pasted from an earlier report.

**How this fails in practice:** When auditing a new codebase, it's tempting to reuse the narrative structure from a previous report. This is fine for the prose, but **any specific file paths or function names will be wrong** — different codebases have different file structures, different endpoint names, different service files. A report that cites `oracle_trigger` and `proactive_support_webhook` when auditing a site that has `invite_parent` and `stripe_webhook` is instantly non-credible.

**Required self-check before finalizing the report:**

For every file:line cited in the report (Location fields, Key Hotspots tables, Prompt templates), grep the current audit output to verify it's actually there:

```bash
# For each cited location, confirm it appears in the CURRENT audit:
grep '<file:line>' /tmp/audit.txt
```

If the citation isn't in the current audit, **don't include it.** Either it's from a previous audit (remove it) or the finding shouldn't be in this report at all.

**Prose framing (analogies, descriptions, general patterns) can be reused** — but every concrete identifier must be freshly extracted.

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
- **Prompt template:** A generic prompt structure with `{placeholders}` the user fills in — not a literal copy-paste prompt. See "Rules for Fix guidance" below for exact format.
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
- **Fix guidance** (NOT exact prompts) — describe *what approach* to take, not what literal prompt to paste
- PASS checks go in a summary table — one row per check, with a brief "Notes" column explaining what it means
- Lighthouse opportunities table must include a plain-language "What it means" column
- Remediation roadmap groups findings by priority with "Expected improvement" column

#### Rules for "Fix guidance"

Users of this audit are typically **non-technical or semi-technical operators**. They need prompt text they can send to their agent — but not prompts so specific that they'll be held against us if the agent fails to act on them.

**The solution: generic prompt templates** — a real prompt structure with placeholders the user fills in for their specific case.

**Structure every finding's fix guidance like this:**

```markdown
**Prompt template** (fill in the placeholders before sending to your agent):

> "{Verb: Fix / Refactor / Optimize} {one-line description of the problem}.
>
> Affected files: {paste the file:line list from the Location section above}
>
> Approach: {describe the strategy in 2-3 lines — e.g., 'batch the queries using `{"_id": {"$in": ids}}` instead of looping', or 'consolidate redirect chain to a single 301 hop'}
>
> Constraints:
> - Don't change {list what to preserve — business logic, UI, auth, response shape}
> - {Any other constraints relevant to this issue}
>
> Verify by: {simple sanity check the agent can run — e.g., 'run the endpoint and confirm same response shape' or 'run curl -IL to confirm single redirect'}"

**Why this is a template not a ready-to-paste prompt:** The `{placeholders}` are intentional — they're meant to be filled in by the operator based on their codebase, agent, and specific situation. The structure is the guidance; the exact wording is the operator's.
```

**Tone rules:**

- Always say "**Prompt template**" — makes it clear this is a starting point, not a canonical prompt
- Always include `{placeholder}` markers — signals that customization is expected
- Always include a "Constraints" line — teaches operators to tell their agent what NOT to change (this is often more important than what to change)
- Always include a "Verify by" line — teaches operators to close the loop after the agent makes changes

**Never use phrases like:**
- "Copy this prompt into your agent" (sounds like a guarantee)
- "Paste the following" (sounds like a guarantee)
- "Use this exact wording" (sounds like a guarantee)

**Instead use:**
- "Prompt template (fill in the placeholders):"
- "Starting point for your agent prompt:"
- "Template — adapt for your case:"

#### Report style rules

- Keep all technical details intact — file paths, line numbers, evidence, check names
- Add plain-language context below/beside technical content, never instead of it
- Use status emojis in tables (🟢 🟡 🔴) for visual scanning
- No jargon without explanation — if you use a technical term (LCP, TBT, N+1, etc.), add a brief note on what it means
- Lead with what's working (PASS checks) before diving into failures where appropriate
- The report should be readable by both a developer (who skips the "In plain terms" blocks) and a non-technical founder (who reads them)

---

### Step 6 — Report Completion

Write the report to `perf-audit/reports/{slug}.md` and present a summary table to the operator. Include:
- Desktop + mobile Lighthouse scores
- Severity counts (Critical / High / Low)
- Top 3 findings
- Location of the full report

---

## Error Handling

- Pod wake fails -> report error, continue with Lighthouse only if preview URL is still reachable
- Static analysis fails -> note in report, continue with Lighthouse-only results
- Lighthouse fails -> skip Step 3b, continue with static-only analysis
- Template is `expo` -> skip Lighthouse, note "N/A (native app)"
- Template not detected -> run as "generic" (all template checks)

## Constraints

- Only report what is directly observable in the codebase or measured by Lighthouse
- If a file cannot be read, note as unverified — don't assume pass or fail
- Do not recommend architectural changes beyond pre-launch scope
