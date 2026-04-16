"""
Performance audit runner — downloads perf_audit.py, runs it, formats PASS/FAIL report.
Output: Check results + fix prompts separated by ===SEPARATOR===
Same architecture as run_audit.py for code quality evals.
"""
import json, os, sys, subprocess
from collections import defaultdict

AUDIT_URL = os.environ.get("PERF_AUDIT_URL", "https://raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_audit.py")
AUDIT_PATH = "/tmp/perf_audit.py"
PROJECT_ROOT = sys.argv[1] if len(sys.argv) > 1 else "/app"

# Download perf_audit.py if missing
if not os.path.exists(AUDIT_PATH):
    os.system(f"curl -sL {AUDIT_URL} -o {AUDIT_PATH}")

# Run audit
result = subprocess.run(
    ["python3", AUDIT_PATH, PROJECT_ROOT],
    capture_output=True, text=True, timeout=120
)
if result.returncode != 0:
    print("error: perf_audit.py failed — " + result.stderr[:200])
    print("===SEPARATOR===")
    print("")
    sys.exit(0)

report = json.loads(result.stdout)
template = report["template"]
findings = report["findings"]

# ── Group findings by check type ──
def check_key(f):
    t = f["title"]
    if "N+1" in t: return "n_plus_1"
    if "Unbounded" in t: return "unbounded_queries"
    if "Sync route" in t: return "async_handlers"
    if "MongoDB client" in t: return "mongo_singleton"
    if "Missing indexes" in t: return "missing_indexes"
    if "Sequential async" in t: return "sequential_async"
    if "Blocking work" in t: return "blocking_handlers"
    if "No projection" in t: return "over_fetching"
    if "Pydantic" in t: return "pydantic_overhead"
    if "ScrollView" in t: return "scrollview_map"
    if "useEffect" in t: return "memory_leaks"
    if "Inline function" in t: return "rerender_patterns"
    if "Heavy package" in t: return "bundle_size"
    if "dimension" in t.lower() or ("Image" in t and "Remote" in t): return "image_loading"
    if "Hermes" in t: return "hermes_engine"
    if "Heavy work" in t: return "heavy_animations"
    if "SDK" in t: return "sdk_compat"
    if "production" in t.lower(): return "production_mode"
    if "getServerSideProps" in t: return "data_fetching"
    if "code split" in t.lower() or "not code-split" in t.lower(): return "code_splitting"
    if "img" in t.lower() and "Next" in t: return "image_optimization"
    if "virtualiz" in t.lower(): return "list_virtualization"
    if "cache" in t.lower(): return "cache_headers"
    if "next.config" in t.lower(): return "nextjs_config"
    if "tree shak" in t.lower(): return "tree_shaking"
    if "static asset" in t.lower() or "unoptimized" in t.lower(): return "static_assets"
    if "Nested iteration" in t: return "algorithmic_complexity"
    if "Array lookup" in t: return "inefficient_data_structures"
    if "Sequential API" in t: return "promise_parallelization"
    return "other"

grouped = defaultdict(list)
for f in findings:
    grouped[check_key(f)].append(f)

# ── Check definitions ──
TMAP = {"nextjs": "Next.js", "expo": "Expo", "farm": "Farm", "generic": "Generic"}

BACKEND = [
    ("async_handlers", "Async Route Handlers", "All route handlers use `async def` — no sync blocking detected."),
    ("n_plus_1", "N+1 Query Detection", None),
    ("unbounded_queries", "Unbounded Queries", None),
    ("mongo_singleton", "MongoDB Client Singleton", "Client is properly initialized at startup — not recreated per request."),
    ("missing_indexes", "Missing Collection Indexes", None),
    ("sequential_async", "Sequential Async Operations", None),
    ("blocking_handlers", "Blocking Work in Request Handlers", None),
    ("over_fetching", "Over-fetching (No Projection)", None),
    ("pydantic_overhead", "Pydantic Validation Overhead", None),
]
NEXTJS = [
    ("production_mode", "Production Mode Configuration", "Production build command is properly configured."),
    ("data_fetching", "Data Fetching Strategy (SSR vs SSG)", "No unnecessary getServerSideProps on static pages."),
    ("code_splitting", "Code Splitting", "Heavy libraries are properly code-split with dynamic imports."),
    ("bundle_size", "Bundle Size / Import Hygiene", "No heavy packages detected."),
    ("image_optimization", "Image Optimization (next/image)", "All images use Next.js Image component."),
    ("rerender_patterns", "Unnecessary Re-renders", None),
    ("list_virtualization", "List Virtualization", "Lists are properly virtualized."),
    ("memory_leaks", "Memory Leaks (useEffect Cleanup)", None),
    ("cache_headers", "Cache Headers on API Routes", None),
    ("nextjs_config", "next.config.js Audit", "Configuration is properly set."),
]
EXPO = [
    ("scrollview_map", "ScrollView with .map() on Lists", None),
    ("memory_leaks", "Memory Leaks (useEffect Cleanup)", None),
    ("rerender_patterns", "Re-renders (Inline Functions)", None),
    ("bundle_size", "Bundle and App Size", "No heavy packages detected in dependencies."),
    ("image_loading", "Image Loading (Dimensions)", None),
    ("hermes_engine", "Hermes Engine", None),
    ("heavy_animations", "Heavy Work During Animations", None),
    ("sdk_compat", "SDK Version Compatibility", "Expo SDK version is acceptable."),
]
FARM = [
    ("tree_shaking", "Bundle Size / Tree Shaking", "No tree-shaking issues detected."),
    ("code_splitting", "Code Splitting", "Heavy libraries are properly code-split."),
    ("rerender_patterns", "Re-render Patterns", None),
    ("static_assets", "Static Asset Optimization", "Assets are properly optimized."),
    ("memory_leaks", "Memory Leaks", None),
]
DB = [
    ("missing_indexes", "Missing Indexes", "All queried fields have corresponding indexes."),
    ("over_fetching", "No Projection on Queries", "All queries use projections."),
]
ALGO = [
    ("algorithmic_complexity", "Algorithmic Complexity", "No nested iterations detected."),
    ("inefficient_data_structures", "Inefficient Data Structures", None),
    ("promise_parallelization", "Promise Parallelization", "No sequential fetch chains detected."),
]

# ── Build check report ──
lines = [f"## Performance Audit — {TMAP.get(template, template)}\n"]

def render(title, checks):
    lines.append(f"### {title}\n")
    p, f_ = 0, 0
    for key, label, pass_msg in checks:
        fl = grouped.get(key, [])
        if not fl:
            p += 1
            lines.append(f"#### PASS — {label}")
            lines.append(f"{pass_msg or 'No issues detected.'}\n")
        else:
            f_ += 1
            lines.append(f"#### FAIL — {label}")
            lines.append(f"{fl[0]['impact']}")
            for x in fl:
                loc = f"{x['file']}:{x['line']}" if x['line'] else x['file']
                lines.append(f"- `{loc}` — {x['title']}")
            lines.append("")
    lines.append(f"**Result: {p} passed, {f_} failed out of {p+f_} checks**\n---\n")
    return p, f_

bp, bf = render("1. Backend Performance Checks", BACKEND)
tpl = {"nextjs": NEXTJS, "expo": EXPO, "farm": FARM}.get(template, EXPO + NEXTJS)
tpl_name = TMAP.get(template, template)
rp, rf = render(f"2. Rendering Performance Checks ({tpl_name})", tpl)
dp, df = render("3. Database Checks", DB)
ap, af = render("4. Algorithm & Code Quality Checks", ALGO)

lines.append("### Summary\n")
lines.append("| Check Category | Pass | Fail | Total |")
lines.append("|----------------|------|------|-------|")
lines.append(f"| Backend | {bp} | {bf} | {bp+bf} |")
lines.append(f"| Rendering ({tpl_name}) | {rp} | {rf} | {rp+rf} |")
lines.append(f"| Database | {dp} | {df} | {dp+df} |")
lines.append(f"| Algorithms | {ap} | {af} | {ap+af} |")
tp, tf = bp+rp+dp+ap, bf+rf+df+af
lines.append(f"| **Total** | **{tp}** | **{tf}** | **{tp+tf}** |")

# ── Build fix prompts ──
prompts = ["\n\n### Fix Prompts\n", "Each prompt is self-contained. Send one at a time to your agent.\n"]
all_c = BACKEND + tpl + DB + ALGO
pn, seen = 0, set()
for key, label, _ in all_c:
    if key in seen: continue
    seen.add(key)
    fl = grouped.get(key, [])
    if not fl: continue
    pn += 1
    locs = [f"{x['file']}:{x['line']}" if x['line'] else x['file'] for x in fl]
    ls = ", ".join(locs[:5]) + (f", and {len(locs)-5} more" if len(locs) > 5 else "")
    prompts.append(f"---\n**Prompt {pn} — {label}**\n")
    prompts.append(f"> \"{fl[0]['fix']}. Affected files: {ls}. Do not change any other logic.\"\n")
prompts.append(f"\n**{pn} fix prompts generated** covering all failing checks.")

print("\n".join(lines))
print("===SEPARATOR===")
print("\n".join(prompts))
