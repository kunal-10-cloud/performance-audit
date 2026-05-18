"""
perf_run_audit.py — runner for the perf-audit-optimised pipeline.

Architecture: this script is hosted on GitHub at
  https://raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_run_audit.py
and downloaded onto the customer's pod at audit time via a single curl. It
then downloads perf_audit.py (the analyzer) from the same repo and runs both
modes against the project root.

Two modes:

  --mode=analyze   Download perf_audit.py, run it, emit PASS/FAIL report +
                   legacy fix-prompt block separated by ===SEPARATOR===
                   (kept for backward compatibility with the legacy pipeline;
                   the optimised pipeline's Pass A reads the report half and
                   ignores the fix-prompt half).

  --mode=facts     Run a fixed set of deterministic grep recipes on the
                   project root and emit a single JSON dict to stdout. Keys
                   map 1:1 to the negative-claim references in
                   references.md (create_index_count, react_lazy_count,
                   async_handler_count, etc.). Extend run_facts() to add
                   new fields.

Usage on the pod:
  python3 perf_run_audit.py --mode=analyze /app   > /tmp/audit.txt
  python3 perf_run_audit.py --mode=facts   /app   > /tmp/facts.json

Or the legacy positional form (defaults to analyze for back-compat):
  python3 perf_run_audit.py /app                  > /tmp/audit.txt
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone


# ══════════════════════════════════════════════════════════════════════
#  ANALYZER FETCH + RUN
# ══════════════════════════════════════════════════════════════════════

AUDIT_URL = os.environ.get(
    "PERF_AUDIT_URL",
    "https://raw.githubusercontent.com/kunal-10-cloud/performance-audit/main/perf_audit.py",
)
AUDIT_PATH = "/tmp/perf_audit.py"


def _check_key(title):
    t = title
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
    if "viewport" in t.lower() and "meta" in t.lower(): return "meta_viewport"
    if "viewport" in t.lower() and "incomplete" in t.lower(): return "meta_viewport"
    if "media quer" in t.lower(): return "media_query_coverage"
    if "mobile-width" in t.lower(): return "media_query_coverage"
    if "touch target" in t.lower(): return "touch_targets"
    if "100vh" in t or "viewport unit" in t.lower(): return "viewport_units"
    return "other"


_TMAP = {"nextjs": "Next.js", "expo": "Expo", "farm": "Farm", "generic": "Generic"}

_BACKEND = [
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
_NEXTJS = [
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
_EXPO = [
    ("scrollview_map", "ScrollView with .map() on Lists", None),
    ("memory_leaks", "Memory Leaks (useEffect Cleanup)", None),
    ("rerender_patterns", "Re-renders (Inline Functions)", None),
    ("bundle_size", "Bundle and App Size", "No heavy packages detected in dependencies."),
    ("image_loading", "Image Loading (Dimensions)", None),
    ("hermes_engine", "Hermes Engine", None),
    ("heavy_animations", "Heavy Work During Animations", None),
    ("sdk_compat", "SDK Version Compatibility", "Expo SDK version is acceptable."),
]
_FARM = [
    ("tree_shaking", "Bundle Size / Tree Shaking", "No tree-shaking issues detected."),
    ("code_splitting", "Code Splitting", "Heavy libraries are properly code-split."),
    ("rerender_patterns", "Re-render Patterns", None),
    ("static_assets", "Static Asset Optimization", "Assets are properly optimized."),
    ("memory_leaks", "Memory Leaks", None),
]
_DB = [
    ("missing_indexes", "Missing Indexes", "All queried fields have corresponding indexes."),
    ("over_fetching", "No Projection on Queries", "All queries use projections."),
]
_ALGO = [
    ("algorithmic_complexity", "Algorithmic Complexity", "No nested iterations detected."),
    ("inefficient_data_structures", "Inefficient Data Structures", None),
    ("promise_parallelization", "Promise Parallelization", "No sequential fetch chains detected."),
]
_MOBILE = [
    ("meta_viewport", "Meta Viewport Tag", "Viewport meta tag is properly configured."),
    ("media_query_coverage", "Responsive Media Queries", "Mobile-width breakpoints detected in stylesheets."),
    ("touch_targets", "Touch Target Sizes", "All interactive elements meet minimum touch target size (44px)."),
    ("viewport_units", "Viewport Unit Usage (100vh)", "No problematic 100vh usage detected."),
]


def run_analyze(project_root):
    """Download perf_audit.py if needed, run it, and print PASS/FAIL markdown to stdout."""
    if not os.path.exists(AUDIT_PATH):
        os.system(f"curl -sL {AUDIT_URL} -o {AUDIT_PATH}")

    result = subprocess.run(
        ["python3", AUDIT_PATH, project_root],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print("error: perf_audit.py failed — " + result.stderr[:200])
        print("===SEPARATOR===")
        print("")
        return

    report = json.loads(result.stdout)
    template = report["template"]
    findings = report["findings"]

    grouped = defaultdict(list)
    for f in findings:
        grouped[_check_key(f["title"])].append(f)

    lines = [f"## Performance Audit — {_TMAP.get(template, template)}\n"]

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

    bp, bf = render("1. Backend Performance Checks", _BACKEND)
    tpl = {"nextjs": _NEXTJS, "expo": _EXPO, "farm": _FARM}.get(template, _EXPO + _NEXTJS)
    tpl_name = _TMAP.get(template, template)
    rp, rf = render(f"2. Rendering Performance Checks ({tpl_name})", tpl)
    dp, df_ = render("3. Database Checks", _DB)
    ap, af = render("4. Algorithm & Code Quality Checks", _ALGO)
    mp, mf = (0, 0)
    if template != "expo":
        mp, mf = render("5. Mobile & Responsive Checks", _MOBILE)

    lines.append("### Summary\n")
    lines.append("| Check Category | Pass | Fail | Total |")
    lines.append("|----------------|------|------|-------|")
    lines.append(f"| Backend | {bp} | {bf} | {bp+bf} |")
    lines.append(f"| Rendering ({tpl_name}) | {rp} | {rf} | {rp+rf} |")
    lines.append(f"| Database | {dp} | {df_} | {dp+df_} |")
    lines.append(f"| Algorithms | {ap} | {af} | {ap+af} |")
    if template != "expo":
        lines.append(f"| Mobile & Responsive | {mp} | {mf} | {mp+mf} |")
    tp = bp + rp + dp + ap + mp
    tf = bf + rf + df_ + af + mf
    lines.append(f"| **Total** | **{tp}** | **{tf}** | **{tp+tf}** |")

    # Legacy fix-prompt block — kept for backward compatibility. Pass A in the
    # optimised pipeline ignores this section.
    prompts = ["\n\n### Fix Prompts\n", "Each prompt is self-contained. Send one at a time to your agent.\n"]
    all_c = _BACKEND + tpl + _DB + _ALGO + (_MOBILE if template != "expo" else [])
    pn, seen = 0, set()
    for key, label, _ in all_c:
        if key in seen:
            continue
        seen.add(key)
        fl = grouped.get(key, [])
        if not fl:
            continue
        pn += 1
        locs = [f"{x['file']}:{x['line']}" if x['line'] else x['file'] for x in fl]
        ls = ", ".join(locs[:5]) + (f", and {len(locs)-5} more" if len(locs) > 5 else "")
        prompts.append(f"---\n**Prompt {pn} — {label}**\n")
        prompts.append(f"> \"{fl[0]['fix']}. Affected files: {ls}. Do not change any other logic.\"\n")
    prompts.append(f"\n**{pn} fix prompts generated** covering all failing checks.")

    print("\n".join(lines))
    print("===SEPARATOR===")
    print("\n".join(prompts))


# ══════════════════════════════════════════════════════════════════════
#  FACTS MODE (deterministic grep recipes for facts.json)
# ══════════════════════════════════════════════════════════════════════

def _sh_quote(s):
    """Quote s for POSIX shell without double-escaping backslashes (which is
    what repr() would do — single-quoted shell text preserves backslashes
    literally, so we just wrap in single quotes and escape any embedded
    single quotes)."""
    return "'" + s.replace("'", "'\\''") + "'"


def _grep_count(pattern, path, includes=None):
    """Count occurrences of regex pattern across files under path.
    includes: optional list of file globs, e.g. ['*.py']. Returns int."""
    if not os.path.isdir(path):
        return 0
    flags = " ".join(f"--include={_sh_quote(g)}" for g in (includes or []))
    cmd = f"grep -rE {flags} {_sh_quote(pattern)} {_sh_quote(path)} 2>/dev/null | wc -l"
    try:
        out = subprocess.check_output(cmd, shell=True, text=True, timeout=30, executable="/bin/bash")
        return int(out.strip())
    except Exception:
        return 0


def _grep_files(pattern, path, includes=None):
    """Return list of files containing pattern under path."""
    if not os.path.isdir(path):
        return []
    flags = " ".join(f"--include={_sh_quote(g)}" for g in (includes or []))
    cmd = f"grep -rlE {flags} {_sh_quote(pattern)} {_sh_quote(path)} 2>/dev/null"
    try:
        out = subprocess.check_output(cmd, shell=True, text=True, timeout=30, executable="/bin/bash")
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _read_file_safe_str(path, max_bytes=200_000):
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _detect_framework_signature(project_root):
    parts = []
    pkg_path = os.path.join(project_root, "frontend", "package.json")
    if not os.path.isfile(pkg_path):
        pkg_path = os.path.join(project_root, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r") as fh:
                pkg = json.load(fh)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps: parts.append("nextjs")
            elif "react-scripts" in deps: parts.append("cra")
            elif "vite" in deps: parts.append("vite")
            elif "expo" in deps: parts.append("expo")
            elif "react" in deps: parts.append("react")
        except Exception:
            pass
    req_path = os.path.join(project_root, "backend", "requirements.txt")
    if not os.path.isfile(req_path):
        req_path = os.path.join(project_root, "requirements.txt")
    if os.path.isfile(req_path):
        req = _read_file_safe_str(req_path)
        if re.search(r"^fastapi\b", req, re.MULTILINE): parts.append("fastapi")
        if re.search(r"^motor\b", req, re.MULTILINE): parts.append("motor")
        if re.search(r"^django\b", req, re.MULTILINE): parts.append("django")
        if re.search(r"^flask\b", req, re.MULTILINE): parts.append("flask")
    return "+".join(parts) if parts else "unknown"


def _detect_pydantic_major(project_root):
    req_path = os.path.join(project_root, "backend", "requirements.txt")
    if not os.path.isfile(req_path):
        req_path = os.path.join(project_root, "requirements.txt")
    if not os.path.isfile(req_path):
        return None
    req = _read_file_safe_str(req_path)
    m = re.search(r"^pydantic\b\s*[=><~]+\s*(\d+)", req, re.MULTILINE)
    if m:
        return int(m.group(1))
    if re.search(r"^pydantic\b", req, re.MULTILINE):
        return 2
    return None


def _detect_tailwind(project_root):
    for pkg_path in [
        os.path.join(project_root, "frontend", "package.json"),
        os.path.join(project_root, "package.json"),
    ]:
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path, "r") as fh:
                    pkg = json.load(fh)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "tailwindcss" in deps:
                    return True
            except Exception:
                pass
    return False


def _detect_deployed_bundle(project_root):
    candidates = [
        os.path.join(project_root, "frontend", "build", "index.html"),
        os.path.join(project_root, "frontend", "dist", "index.html"),
        os.path.join(project_root, "build", "index.html"),
        os.path.join(project_root, "dist", "index.html"),
    ]
    html = ""
    html_path = None
    for c in candidates:
        if os.path.isfile(c):
            html = _read_file_safe_str(c)
            html_path = c
            break
    if not html:
        return {"deployed_bundle_hash": None, "deployed_bundle_size_bytes": None}

    m = re.search(r'/static/js/(main\.([0-9a-f]+)\.js)', html)
    if not m:
        m = re.search(r'/assets/(index-([0-9a-zA-Z_-]+)\.js)', html)
    if not m:
        return {"deployed_bundle_hash": None, "deployed_bundle_size_bytes": None}

    fname, hash_ = m.group(1), m.group(2)
    build_dir = os.path.dirname(html_path)
    for sub in ["static/js", "assets", ""]:
        candidate = os.path.join(build_dir, sub, fname)
        if os.path.isfile(candidate):
            return {
                "deployed_bundle_hash": hash_,
                "deployed_bundle_size_bytes": os.path.getsize(candidate),
            }
    return {"deployed_bundle_hash": hash_, "deployed_bundle_size_bytes": None}


def _list_indexed_collections(project_root):
    backend = os.path.join(project_root, "backend")
    if not os.path.isdir(backend):
        return []
    cmd = (
        f"grep -rnE 'await\\s+(db|database|self\\.db|self\\.database)\\.[a-zA-Z_]+\\.create_index' "
        f"{backend} 2>/dev/null"
    )
    try:
        out = subprocess.check_output(cmd, shell=True, text=True, timeout=30)
    except Exception:
        return []
    collections = set()
    pattern = re.compile(r"\.(?P<coll>[a-zA-Z_][a-zA-Z0-9_]*)\.create_index")
    for line in out.splitlines():
        m = pattern.search(line)
        if m:
            collections.add(m.group("coll"))
    return sorted(collections)


def run_facts(project_root):
    """Gather pinned codebase facts deterministically. Print as JSON to stdout."""
    backend = os.path.join(project_root, "backend")
    frontend_src = os.path.join(project_root, "frontend", "src")
    if not os.path.isdir(frontend_src):
        frontend_src = os.path.join(project_root, "src")
    routes = os.path.join(backend, "routes")

    bundle_info = _detect_deployed_bundle(project_root)

    facts = {
        "audit_id": os.environ.get("AUDIT_SLUG", os.path.basename(os.path.abspath(project_root))),
        "facts_gathered_at": datetime.now(timezone.utc).isoformat(),
        "project_root": os.path.abspath(project_root),
        "framework_signature": _detect_framework_signature(project_root),
        "pydantic_major_version": _detect_pydantic_major(project_root),
        "tailwind_present": _detect_tailwind(project_root),
        "create_index_count": _grep_count(r"\.create_index\s*\(", backend, includes=["*.py"]),
        "indexed_collections": _list_indexed_collections(project_root),
        "async_handler_count": _grep_count(
            r"^\s*async\s+def\s+\w+", backend, includes=["*.py"]
        ),
        "route_declaration_count": _grep_count(
            r"@(app|router)\.(get|post|put|delete|patch)\s*\(",
            routes if os.path.isdir(routes) else backend,
            includes=["*.py"],
        ),
        "create_index_call_files": _grep_files(
            r"\.create_index\s*\(", backend, includes=["*.py"]
        ),
        "react_lazy_count": _grep_count(
            r"React\.lazy\s*\(|=\s*lazy\s*\(", frontend_src,
            includes=["*.js", "*.jsx", "*.ts", "*.tsx"],
        ),
        "source_lazy_call_files": _grep_files(
            r"React\.lazy\s*\(|=\s*lazy\s*\(", frontend_src,
            includes=["*.js", "*.jsx", "*.ts", "*.tsx"],
        ),
        "useeffect_cleanup_count": _grep_count(
            r"return\s*\(\s*\)\s*=>", frontend_src,
            includes=["*.js", "*.jsx", "*.ts", "*.tsx"],
        ),
        "virtualization_lib_imports": _grep_count(
            r"from\s+['\"](react-window|react-virtualized|@tanstack/react-virtual|react-virtual)['\"]",
            frontend_src,
            includes=["*.js", "*.jsx", "*.ts", "*.tsx"],
        ),
        "deployed_bundle_hash": bundle_info["deployed_bundle_hash"],
        "deployed_bundle_size_bytes": bundle_info["deployed_bundle_size_bytes"],
        "presence": {
            "backend_dir": os.path.isdir(backend),
            "backend_routes_dir": os.path.isdir(routes),
            "backend_services_dir": os.path.isdir(os.path.join(backend, "services")),
            "backend_migrations_dir": os.path.isdir(os.path.join(backend, "migrations")),
            "backend_scripts_dir": os.path.isdir(os.path.join(backend, "scripts")),
            "backend_models_dir": os.path.isdir(os.path.join(backend, "models")),
            "frontend_src_dir": os.path.isdir(frontend_src),
            "frontend_build_dir": os.path.isdir(os.path.join(project_root, "frontend", "build")),
            "frontend_dist_dir": os.path.isdir(os.path.join(project_root, "frontend", "dist")),
        },
    }

    print(json.dumps(facts, indent=2, sort_keys=True))


# ══════════════════════════════════════════════════════════════════════
#  CLI DISPATCHER
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="perf-audit-optimised runner")
    parser.add_argument("--mode", choices=["analyze", "facts"], default="analyze")
    parser.add_argument("project_root", nargs="?", default="/app")
    args = parser.parse_args()

    if args.mode == "analyze":
        run_analyze(args.project_root)
    elif args.mode == "facts":
        run_facts(args.project_root)


if __name__ == "__main__":
    main()
