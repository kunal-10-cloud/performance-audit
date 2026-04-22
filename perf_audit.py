"""
Emergent Performance Audit Script
===================================
Static performance analysis for React+FastAPI+MongoDB apps built on Emergent.
Detects template (Next.js/Expo/Farm), runs backend + template-specific + cross-cutting
performance checks, scores, and outputs structured JSON.

Usage:
    python perf_audit.py [project_root]
    python perf_audit.py /app

Output: JSON report to stdout.
"""

import ast
import os
import re
import json
import sys
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IGNORE_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".git", ".next", "env", "site-packages", "eggs",
    ".tox", ".mypy_cache", ".pytest_cache", "coverage", ".coverage", "htmlcov",
    ".emergent", "components/ui", "tmp", ".tmp",
}

EXCLUDE_ANALYSIS_DIRS = {
    "tests", "test", "__tests__", "testing",
    "examples", "example", "demos", "demo",
    "docs", "doc", "documentation",
    "scripts", "script", "tools", "fixtures",
    "migrations", "seeds", "stubs", "mocks",
    "benchmarks", "benchmark",
}

BACKEND_EXTENSIONS = {".py"}
FRONTEND_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}

# Known heavy packages that hurt bundle size
HEAVY_PACKAGES = {
    "moment": "date-fns or dayjs",
    "lodash": "lodash-es or individual lodash/* imports",
    "@mui/material": "individual @mui/* component imports",
    "jquery": "remove — use native DOM APIs",
    "rxjs": "consider lighter alternatives for simple use cases",
    "chart.js": "use dynamic import",
    "three": "use dynamic import",
    "monaco-editor": "use dynamic import",
    "pdf-lib": "use dynamic import",
    "xlsx": "use dynamic import",
}

# MongoDB query methods that indicate DB calls
MONGO_QUERY_METHODS = {
    "find_one", "find", "aggregate", "count_documents",
    "distinct", "insert_one", "insert_many", "update_one",
    "update_many", "delete_one", "delete_many", "replace_one",
}

# Blocking service patterns (should be in background tasks)
BLOCKING_PATTERNS = [
    r"send_email|send_mail|smtp|sendgrid|ses\.send|resend\.",
    r"send_notification|push_notification|fcm\.|firebase_admin\.messaging",
    r"webhook|httpx\.post|requests\.post|aiohttp.*post",
    r"analytics|track_event|segment\.",
    r"stripe\.|paypal\.|razorpay\.",
]

PROJECT_ROOT = sys.argv[1] if len(sys.argv) > 1 else "/app"

# ---------------------------------------------------------------------------
# Finding counter
# ---------------------------------------------------------------------------
_finding_counter = 0

def new_finding(category, severity, title, file_path, line, description, impact, fix):
    global _finding_counter
    _finding_counter += 1
    return {
        "id": f"PERF-{_finding_counter:03d}",
        "category": category,
        "severity": severity,
        "title": title,
        "file": str(file_path).replace(PROJECT_ROOT + "/", "").replace(PROJECT_ROOT + "\\", ""),
        "line": line,
        "description": description,
        "impact": impact,
        "fix": fix,
    }

# ---------------------------------------------------------------------------
# File Collection
# ---------------------------------------------------------------------------

def collect_files(root):
    """Walk project tree, collect source files, skip ignored dirs."""
    backend_files = []
    frontend_files = []
    all_files = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip ignored directories
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS
            and not any(dirpath.endswith(ex) for ex in IGNORE_DIRS)
        ]

        rel_dir = os.path.relpath(dirpath, root)
        # Skip analysis-excluded dirs for checks (but still collect for dep analysis)
        in_excluded = any(part in EXCLUDE_ANALYSIS_DIRS for part in Path(rel_dir).parts)

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            ext = os.path.splitext(fname)[1].lower()
            rel_path = os.path.relpath(fpath, root)

            if in_excluded:
                continue

            if ext in BACKEND_EXTENSIONS:
                backend_files.append(fpath)
            elif ext in FRONTEND_EXTENSIONS:
                frontend_files.append(fpath)

            all_files.append(fpath)

    return backend_files, frontend_files, all_files


def read_file_safe(fpath, max_size=500_000):
    """Read file content safely with size limit."""
    try:
        size = os.path.getsize(fpath)
        if size > max_size:
            return None
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def read_json_safe(fpath):
    """Read and parse a JSON file safely."""
    content = read_file_safe(fpath)
    if content is None:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# Step 1: Template Detection
# ---------------------------------------------------------------------------

def detect_template(root):
    """Detect which Emergent template the project uses."""
    pkg_json = read_json_safe(os.path.join(root, "package.json"))
    if pkg_json is None:
        # Check frontend subdirectory
        pkg_json = read_json_safe(os.path.join(root, "frontend", "package.json"))

    app_json = read_json_safe(os.path.join(root, "app.json"))
    if app_json is None:
        app_json = read_json_safe(os.path.join(root, "frontend", "app.json"))

    all_deps = {}
    if pkg_json:
        all_deps.update(pkg_json.get("dependencies", {}))
        all_deps.update(pkg_json.get("devDependencies", {}))

    # Check for Next.js
    if "next" in all_deps:
        return "nextjs"
    if os.path.exists(os.path.join(root, "next.config.js")) or \
       os.path.exists(os.path.join(root, "next.config.mjs")) or \
       os.path.exists(os.path.join(root, "frontend", "next.config.js")):
        return "nextjs"

    # Check for Expo
    if "expo" in all_deps:
        return "expo"
    if app_json and ("expo" in app_json or "sdkVersion" in app_json.get("expo", {})):
        return "expo"

    # Check for Farm
    if "@farmfe/core" in all_deps:
        return "farm"
    if os.path.exists(os.path.join(root, "farm.config.ts")) or \
       os.path.exists(os.path.join(root, "farm.config.js")):
        return "farm"

    return "generic"

# ---------------------------------------------------------------------------
# Step 2: Universal Backend Checks (FastAPI + MongoDB)
# ---------------------------------------------------------------------------

def check_async_handlers(backend_files):
    """2.1 — Flag sync route handlers that should be async."""
    findings = []
    # FastAPI route decorators
    route_pattern = re.compile(r'@\w+\.(get|post|put|delete|patch|options|head)\(')

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and not isinstance(node, ast.AsyncFunctionDef):
                # Check if this function has a route decorator
                for decorator in node.decorator_list:
                    dec_str = ast.dump(decorator)
                    if any(method in dec_str for method in ['get', 'post', 'put', 'delete', 'patch']):
                        # Check if it's a high-frequency endpoint
                        is_high_freq = any(kw in node.name.lower() for kw in
                                          ['get', 'list', 'search', 'auth', 'login', 'fetch'])
                        severity = "A" if is_high_freq else "B"
                        findings.append(new_finding(
                            "backend_efficiency", severity,
                            f"Sync route handler: {node.name}",
                            fpath, node.lineno,
                            f"Route handler `{node.name}` uses `def` instead of `async def`",
                            "Blocks the event loop during I/O operations, reducing concurrent request handling",
                            f"Change `def {node.name}(...)` to `async def {node.name}(...)` and ensure all DB/IO calls use `await`"
                        ))
                        break

    return findings


def check_n_plus_1(backend_files):
    """2.2 — Detect MongoDB queries inside loops (N+1 pattern)."""
    findings = []
    mongo_call_pattern = re.compile(
        r'\.(find_one|find|aggregate|count_documents|distinct)\s*\('
    )

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        lines = content.split('\n')
        in_loop = False
        loop_indent = 0
        loop_line = 0

        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # Detect loop start
            if stripped.startswith(('for ', 'async for ', 'while ')):
                in_loop = True
                loop_indent = indent
                loop_line = i
            elif in_loop and indent <= loop_indent and stripped and not stripped.startswith('#'):
                in_loop = False

            # Check for DB call inside loop
            if in_loop and mongo_call_pattern.search(line):
                # Get the function name
                func_match = re.search(r'def\s+(\w+)', '\n'.join(lines[max(0,loop_line-20):loop_line]))
                func_name = func_match.group(1) if func_match else "unknown"

                findings.append(new_finding(
                    "backend_efficiency", "A",
                    f"N+1 query in `{func_name}`",
                    fpath, i,
                    f"MongoDB query call inside a loop at line {i} in function `{func_name}`",
                    "Each iteration makes a separate DB call — causes linear scaling with data size",
                    f"Collect all IDs first, then use a single batch query with `{{\"_id\": {{\"$in\": ids}}}}` or equivalent"
                ))

    return findings


def check_unbounded_queries(backend_files):
    """2.3 — Flag .find() calls without any bound (limit() or to_list(N))."""
    findings = []

    # Patterns that indicate a BOUNDED query (any of these means the query is safe):
    # - .limit(N) or .limit(var)
    # - .to_list(N) where N is a positive integer (not None, not length=None)
    # - limit=N as kwarg
    # Truly unbounded: .to_list(length=None), .to_list(None), .to_list()
    bounded_pattern = re.compile(
        r'\.limit\('                              # .limit( anything
        r'|limit\s*='                              # limit= kwarg
        r'|\.to_list\(\s*\d+\s*\)'                 # .to_list(50) — positive integer
        r'|\.to_list\(\s*length\s*=\s*\d+\s*\)'    # .to_list(length=50)
    )

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            # Only match real .find( — NOT .find_one(, .find_one_and_*, find_and_modify, etc.
            # Look for \.find\( not immediately followed by _one or _and
            if not re.search(r'\.find\(', line):
                continue
            if re.search(r'\.find_one|\.find_and_', line):
                continue

            # Check 5 lines of context (query can span multiple lines)
            context_end = min(i + 5, len(lines))
            context = '\n'.join(lines[i-1:context_end])

            if bounded_pattern.search(context):
                continue  # Bounded — skip

            # Extract function name (look backward for the nearest def)
            func_match = None
            for j in range(i-1, max(0, i-30), -1):
                m = re.search(r'(?:async\s+)?def\s+(\w+)', lines[j-1] if j > 0 else '')
                if m:
                    func_match = m.group(1)
                    break

            # Check if this is in a route handler (GET endpoint returning lists)
            is_get = False
            for j in range(i-1, max(0, i-10), -1):
                if '@' in lines[j-1] and '.get(' in lines[j-1]:
                    is_get = True
                    break

            if is_get or func_match:
                name = func_match or "unknown"
                findings.append(new_finding(
                    "backend_efficiency", "A",
                    f"Unbounded query in `{name}`",
                    fpath, i,
                    f"`.find()` call without `.limit()` or explicit `.to_list(N)` in `{name}` returns all matching documents",
                    "Response size grows unbounded as data increases, causing slow responses and high memory usage",
                    f"Add pagination: `skip: int = 0, limit: int = 50` parameters, apply `.skip(skip).limit(limit)` to the query, or use `.to_list(N)` with an explicit max"
                ))

    return findings


def check_mongo_singleton(backend_files):
    """2.4 — Flag MongoDB client instantiation inside route handlers."""
    findings = []
    client_pattern = re.compile(r'(MongoClient|AsyncIOMotorClient)\s*\(')

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check function body for client instantiation
                func_source = ast.get_source_segment(content, node)
                if func_source and client_pattern.search(func_source):
                    # Check if this function has a route decorator
                    has_route = any(
                        any(method in ast.dump(d) for method in ['get', 'post', 'put', 'delete'])
                        for d in node.decorator_list
                    )
                    if has_route:
                        findings.append(new_finding(
                            "backend_efficiency", "A",
                            f"MongoDB client created inside `{node.name}`",
                            fpath, node.lineno,
                            f"MongoClient/AsyncIOMotorClient is instantiated inside route handler `{node.name}`",
                            "Creates a new database connection pool on every request — causes connection exhaustion and latency",
                            "Move client instantiation to application startup as a singleton. Initialize once at module level or in a startup event."
                        ))

    return findings


def check_missing_indexes(backend_files):
    """2.5 — Flag frequently queried fields without index definitions."""
    findings = []
    queried_fields = defaultdict(set)  # collection -> set of fields
    has_index = defaultdict(set)  # collection -> set of indexed fields

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        # Find queried fields in .find() filters
        find_pattern = re.compile(r'\.find\(\s*\{([^}]+)\}')
        for match in find_pattern.finditer(content):
            filter_str = match.group(1)
            # Extract field names (keys before :)
            field_matches = re.findall(r'"(\w+)"\s*:', filter_str)
            for field in field_matches:
                if field not in ('_id', '$in', '$or', '$and', '$gt', '$lt', '$gte', '$lte'):
                    queried_fields[fpath].add(field)

        # Find create_index calls
        idx_pattern = re.compile(r'create_index\(\s*["\'](\w+)')
        for match in idx_pattern.finditer(content):
            has_index[fpath].add(match.group(1))

    # Find fields that are queried but not indexed
    for fpath, fields in queried_fields.items():
        indexed = has_index.get(fpath, set())
        missing = fields - indexed - {'_id'}
        if missing:
            findings.append(new_finding(
                "backend_efficiency", "B",
                f"Missing indexes for queried fields",
                fpath, 0,
                f"Fields {', '.join(sorted(missing))} are used in query filters but have no corresponding index",
                "Queries on unindexed fields cause full collection scans — slow on large collections",
                f"Add indexes: `db.collection.create_index('{list(missing)[0]}')` for each frequently queried field"
            ))

    return findings


def check_sequential_async(backend_files):
    """2.6 — Flag consecutive await statements that could use asyncio.gather()."""
    findings = []

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        lines = content.split('\n')
        consecutive_awaits = []

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('await ') or '= await ' in stripped:
                consecutive_awaits.append(i)
            else:
                if len(consecutive_awaits) >= 3:
                    # Check if they're independent (heuristic: different variable names)
                    await_lines = [lines[j-1].strip() for j in consecutive_awaits]
                    # Simple heuristic: if variables assigned are different and not used in subsequent awaits
                    assigned_vars = []
                    for al in await_lines:
                        if '=' in al:
                            var = al.split('=')[0].strip()
                            assigned_vars.append(var)

                    if len(set(assigned_vars)) == len(assigned_vars) and len(assigned_vars) >= 2:
                        findings.append(new_finding(
                            "backend_efficiency", "B",
                            f"Sequential async operations",
                            fpath, consecutive_awaits[0],
                            f"{len(consecutive_awaits)} consecutive await statements that may be independent",
                            "Sequential awaits wait for each to complete before starting the next — total time is sum of all",
                            "Use `asyncio.gather()` to run independent async operations in parallel"
                        ))

                consecutive_awaits = []
                if stripped.startswith('await ') or '= await ' in stripped:
                    consecutive_awaits.append(i)

    return findings


def check_blocking_handlers(backend_files):
    """2.7 — Flag blocking work in request handlers (should be background tasks)."""
    findings = []
    blocking_re = re.compile('|'.join(BLOCKING_PATTERNS), re.IGNORECASE)

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check if this is a route handler
                has_route = any(
                    any(method in ast.dump(d) for method in ['get', 'post', 'put', 'delete'])
                    for d in node.decorator_list
                )
                if not has_route:
                    continue

                func_source = ast.get_source_segment(content, node)
                if func_source and blocking_re.search(func_source):
                    # Check if it's wrapped in BackgroundTasks
                    if 'BackgroundTasks' not in func_source and 'background_task' not in func_source.lower():
                        findings.append(new_finding(
                            "backend_efficiency", "B",
                            f"Blocking work in `{node.name}`",
                            fpath, node.lineno,
                            f"Email/notification/webhook call found in route handler `{node.name}` before response returns",
                            "Blocks the response until the external service call completes — adds latency to every request",
                            "Move to FastAPI BackgroundTasks or an async task queue"
                        ))

    return findings


def check_over_fetching(backend_files):
    """2.8 — Flag .find() queries without projection."""
    findings = []

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        # Find .find() calls and check for projection (second argument)
        # Pattern: .find({filter}) without second arg or projection param
        find_calls = re.finditer(r'\.find\(\s*(\{[^}]*\})\s*\)', content)
        for match in find_calls:
            line_num = content[:match.start()].count('\n') + 1
            # This is a find() with only a filter, no projection
            findings.append(new_finding(
                "backend_efficiency", "C",
                "No projection on query",
                fpath, line_num,
                "`.find()` returns all fields from matching documents without a projection",
                "Transfers unnecessary data from database — wastes bandwidth and memory for large documents",
                "Add a projection as second argument: `.find(filter, {\"field1\": 1, \"field2\": 1})` to return only needed fields"
            ))

    return findings[:10]  # Cap at 10 to avoid noise


def check_pydantic_overhead(backend_files):
    """2.9 — Flag deeply nested Pydantic models on hot paths."""
    findings = []

    for fpath in backend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        # Find Pydantic model classes and check nesting depth
        model_classes = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                is_pydantic = any(
                    isinstance(base, ast.Name) and base.id == 'BaseModel'
                    for base in node.bases
                )
                if is_pydantic:
                    # Count fields that reference other models
                    nested_count = 0
                    for item in node.body:
                        if isinstance(item, ast.AnnAssign) and item.annotation:
                            ann_str = ast.dump(item.annotation)
                            if 'List' in ann_str or 'Optional' in ann_str:
                                nested_count += 1
                    model_classes[node.name] = nested_count

        # Flag models with 3+ nested references used in route handlers
        for model_name, depth in model_classes.items():
            if depth >= 3:
                findings.append(new_finding(
                    "backend_efficiency", "D",
                    f"Complex Pydantic model: {model_name}",
                    fpath, 0,
                    f"Pydantic model `{model_name}` has {depth}+ nested/list fields",
                    "Deep validation on every request adds CPU overhead — noticeable on high-frequency endpoints",
                    "Consider using simpler dict-based validation for internal endpoints, or split into smaller models"
                ))

    return findings

# ---------------------------------------------------------------------------
# Step 3: Template-Specific Checks
# ---------------------------------------------------------------------------

# ===== Template A: Next.js =====

def nextjs_checks(frontend_files, root):
    """Run all Next.js specific performance checks."""
    findings = []
    pkg_json = read_json_safe(os.path.join(root, "package.json")) or \
               read_json_safe(os.path.join(root, "frontend", "package.json")) or {}
    all_deps = {**pkg_json.get("dependencies", {}), **pkg_json.get("devDependencies", {})}

    # Only run truly Next.js-specific checks if Next.js is actually in use
    is_nextjs = "next" in all_deps or any(
        os.path.exists(os.path.join(root, n)) or os.path.exists(os.path.join(root, "frontend", n))
        for n in ("next.config.js", "next.config.mjs", "next.config.ts")
    )

    # 3A.1 Production Mode Configuration — Next.js only
    if is_nextjs:
        for name in ["Dockerfile", "docker-compose.yml", "package.json"]:
            for search_root in [root, os.path.join(root, "frontend")]:
                fpath = os.path.join(search_root, name)
                content = read_file_safe(fpath)
                if content and 'next dev' in content and name != "package.json":
                    findings.append(new_finding(
                        "rendering_performance", "A",
                        "Production using `next dev`",
                        fpath, 0,
                        "`next dev` found in deployment/production config",
                        "Development mode disables all optimizations — pages load 10-50x slower",
                        "Use `next build && next start` for production"
                    ))

    # 3A.2 Data Fetching Strategy — Next.js only (getServerSideProps is Next-specific)
    if is_nextjs:
        for fpath in frontend_files:
            content = read_file_safe(fpath)
            if not content:
                continue
            if 'getServerSideProps' in content:
                # Check if the data could be static
                is_static_data = any(kw in content.lower() for kw in
                                   ['config', 'settings', 'about', 'faq', 'terms', 'privacy', 'landing'])
                if is_static_data:
                    line = content[:content.index('getServerSideProps')].count('\n') + 1
                    findings.append(new_finding(
                        "rendering_performance", "B",
                        "getServerSideProps on static page",
                        fpath, line,
                        "`getServerSideProps` used on a page with near-static data",
                        "Forces server rendering on every request instead of serving cached static HTML",
                        "Switch to `getStaticProps` with ISR (`revalidate: 60`) for pages with infrequently changing data"
                    ))

    # 3A.3 Code Splitting
    heavy_import_pattern = re.compile(
        r"import\s+.*from\s+['\"](?:chart\.js|recharts|@nivo|leaflet|mapbox|monaco-editor|pdf-lib|quill|draft-js|three)['\"]"
    )
    dynamic_import_pattern = re.compile(r"dynamic\s*\(\s*\(\)\s*=>\s*import")
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        for match in heavy_import_pattern.finditer(content):
            # Check if there's a dynamic import nearby
            if not dynamic_import_pattern.search(content):
                line = content[:match.start()].count('\n') + 1
                findings.append(new_finding(
                    "rendering_performance", "B",
                    "Heavy library not code-split",
                    fpath, line,
                    f"Static import of heavy library: `{match.group().strip()}`",
                    "Increases initial bundle size — all users download the library even if they don't use the feature",
                    "Use `dynamic(() => import('...'), { ssr: false })` to load lazily"
                ))

    # 3A.4 Bundle Size — Import Hygiene
    for pkg_name, alternative in HEAVY_PACKAGES.items():
        if pkg_name in all_deps:
            findings.append(new_finding(
                "rendering_performance", "B",
                f"Heavy package: {pkg_name}",
                "package.json", 0,
                f"`{pkg_name}` found in dependencies",
                f"Adds significant bundle size — consider lighter alternatives",
                f"Replace with {alternative}"
            ))

    # 3A.5 Image Optimization
    # Only run Next.js Image recommendation if project actually uses Next.js.
    # For non-Next.js React projects (CRA, Vite), recommend responsive srcset + lazy loading.
    is_nextjs = "next" in all_deps or any(
        os.path.exists(os.path.join(root, n)) or os.path.exists(os.path.join(root, "frontend", n))
        for n in ("next.config.js", "next.config.mjs", "next.config.ts")
    )
    img_tag_pattern = re.compile(r'<img\s', re.IGNORECASE)
    img_findings_count = 0
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        for match in img_tag_pattern.finditer(content):
            # Skip if the img tag already has loading="lazy"
            tag_end = content.find('>', match.start())
            tag = content[match.start():tag_end] if tag_end > 0 else ''
            if 'loading=' in tag and 'lazy' in tag:
                continue  # Already lazy-loaded — not a finding
            line = content[:match.start()].count('\n') + 1
            if is_nextjs:
                findings.append(new_finding(
                    "rendering_performance", "B",
                    "Native <img> instead of Next.js Image",
                    fpath, line,
                    "Native `<img>` tag used instead of Next.js `<Image>` component",
                    "Misses automatic lazy loading, WebP conversion, and responsive sizing",
                    "Replace with `import Image from 'next/image'` and use `<Image>` component"
                ))
            else:
                findings.append(new_finding(
                    "rendering_performance", "B",
                    "Native <img> without optimization",
                    fpath, line,
                    "Native `<img>` tag without `loading='lazy'`, `srcset`, or modern format (WebP)",
                    "Images load eagerly and at full resolution — wastes bandwidth and slows LCP",
                    "Add `loading='lazy'` for below-the-fold images, use `srcset` for responsive variants, and serve WebP where possible"
                ))
            img_findings_count += 1
            if img_findings_count >= 10:
                break
        if img_findings_count >= 10:
            break

    # 3A.6 Unnecessary Re-renders
    findings.extend(_check_rerender_patterns(frontend_files))

    # 3A.7 List Virtualization
    findings.extend(_check_list_virtualization(frontend_files))

    # 3A.8 Memory Leaks
    findings.extend(_check_memory_leaks(frontend_files))

    # 3A.9 Cache Headers on API Routes
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        if '/api/' in fpath or 'route.ts' in fpath or 'route.js' in fpath:
            if 'Cache-Control' not in content and 'cache-control' not in content:
                if 'GET' in content or 'export async function' in content:
                    findings.append(new_finding(
                        "rendering_performance", "C",
                        "Missing cache headers on API route",
                        fpath, 0,
                        "API route returns data without Cache-Control headers",
                        "Every request hits the server — no browser or CDN caching",
                        "Add `Cache-Control: public, s-maxage=60, stale-while-revalidate=300` for non-user-specific data"
                    ))

    # 3A.10 next.config.js Audit
    for config_name in ['next.config.js', 'next.config.mjs']:
        for search_root in [root, os.path.join(root, 'frontend')]:
            config_path = os.path.join(search_root, config_name)
            config_content = read_file_safe(config_path)
            if config_content:
                if 'compress: false' in config_content or 'compress:false' in config_content:
                    findings.append(new_finding(
                        "rendering_performance", "C",
                        "Compression disabled in next.config",
                        config_path, 0,
                        "Response compression is disabled in Next.js config",
                        "Responses are sent uncompressed — larger payloads and slower page loads",
                        "Remove `compress: false` or set `compress: true`"
                    ))

    return findings


# ===== Template B: Expo (React Native) =====

def expo_checks(frontend_files, root):
    """Run all Expo/React Native specific performance checks."""
    findings = []
    pkg_json = read_json_safe(os.path.join(root, "package.json")) or \
               read_json_safe(os.path.join(root, "frontend", "package.json")) or {}
    all_deps = {**pkg_json.get("dependencies", {}), **pkg_json.get("devDependencies", {})}

    # Only run Expo-specific checks if Expo is actually in use
    is_expo = "expo" in all_deps or any(
        os.path.exists(os.path.join(root, n)) or os.path.exists(os.path.join(root, "frontend", n))
        for n in ("app.json", "app.config.js", "app.config.ts")
    ) and "react-native" in all_deps

    # 3B.1 ScrollView with Map on Large Lists
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        if '<ScrollView' in content and '.map(' in content:
            # Check if .map is inside ScrollView (heuristic)
            sv_start = content.find('<ScrollView')
            map_pos = content.find('.map(', sv_start)
            if map_pos > sv_start and map_pos - sv_start < 2000:
                line = content[:map_pos].count('\n') + 1
                findings.append(new_finding(
                    "rendering_performance", "A",
                    "ScrollView with .map() — use FlatList",
                    fpath, line,
                    "`.map()` rendering inside `<ScrollView>` instead of `<FlatList>`",
                    "All items render at once — causes jank, high memory usage, and slow initial load on large lists",
                    "Replace ScrollView+map with `<FlatList data={items} renderItem={...} />` for efficient virtualized rendering"
                ))

    # 3B.2 Memory Leaks (higher severity on mobile)
    findings.extend(_check_memory_leaks(frontend_files, severity="A"))

    # 3B.3 Re-renders (extra attention to FlatList renderItem)
    findings.extend(_check_rerender_patterns(frontend_files, check_flatlist=True))

    # 3B.4 Bundle and App Size
    for pkg_name, alternative in HEAVY_PACKAGES.items():
        if pkg_name in all_deps:
            findings.append(new_finding(
                "rendering_performance", "B",
                f"Heavy package in mobile app: {pkg_name}",
                "package.json", 0,
                f"`{pkg_name}` in dependencies increases app download size",
                "Mobile users on slow connections may abandon large app downloads",
                f"Replace with {alternative}"
            ))

    # 3B.5 Image Loading
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        # Check for Image without dimensions
        img_pattern = re.compile(r'<Image[^>]*source\s*=\s*\{[^}]*uri')
        for match in img_pattern.finditer(content):
            context = content[match.start():match.start()+300]
            if 'width' not in context and 'height' not in context:
                line = content[:match.start()].count('\n') + 1
                findings.append(new_finding(
                    "rendering_performance", "B",
                    "Remote image without dimensions",
                    fpath, line,
                    "Remote `<Image>` loaded without explicit width/height props",
                    "Causes layout shift and unnecessary re-renders as image dimensions are calculated after load",
                    "Add explicit `style={{ width: X, height: Y }}` or use `resizeMode` props"
                ))

    # 3B.6 Hermes Engine — only relevant if Expo is actually in use (React Native)
    if is_expo:
        app_json = read_json_safe(os.path.join(root, "app.json")) or \
                   read_json_safe(os.path.join(root, "frontend", "app.json")) or {}
        app_config = read_file_safe(os.path.join(root, "app.config.js")) or \
                     read_file_safe(os.path.join(root, "frontend", "app.config.js")) or ""

        hermes_enabled = False
        if app_json:
            expo_config = app_json.get("expo", {})
            if expo_config.get("jsEngine") == "hermes":
                hermes_enabled = True
        if '"hermes"' in app_config or "'hermes'" in app_config:
            hermes_enabled = True

        if not hermes_enabled:
            findings.append(new_finding(
                "rendering_performance", "B",
                "Hermes engine not enabled",
                "app.json", 0,
                "Hermes JavaScript engine is not enabled in app config",
                "Missing faster startup times, lower memory usage, and smaller APK size that Hermes provides",
                'Add `"jsEngine": "hermes"` to the `expo` section of app.json'
            ))

    # 3B.7 Heavy Work During Animations — React Native/Expo specific pattern
    # (InteractionManager is RN-only; onPress and navigation.navigate() are RN patterns)
    if is_expo:
        for fpath in frontend_files:
            content = read_file_safe(fpath)
            if not content:
                continue
            if 'navigation' in content.lower() and ('fetch(' in content or 'await ' in content):
                if 'InteractionManager' not in content and 'runAfterInteractions' not in content:
                    # Check if there's API call in navigation handler
                    nav_patterns = ['onPress', 'navigate(', 'navigation.']
                    if any(p in content for p in nav_patterns):
                        findings.append(new_finding(
                            "rendering_performance", "B",
                            "Heavy work during navigation",
                            fpath, 0,
                            "API calls or heavy operations during navigation transitions",
                            "Causes animation jank — frames drop while waiting for async operations",
                            "Defer heavy work with `InteractionManager.runAfterInteractions(() => { /* fetch data */ })`"
                        ))

    # 3B.8 SDK Version Compatibility
    if 'expo' in all_deps:
        expo_version = all_deps.get('expo', '')
        # Check for known problematic SDK versions
        if expo_version and ('48' in expo_version or '47' in expo_version):
            findings.append(new_finding(
                "rendering_performance", "C",
                "Outdated Expo SDK",
                "package.json", 0,
                f"Expo SDK version {expo_version} may have known performance issues",
                "Older SDKs miss performance optimizations and may have compatibility issues with newer native modules",
                "Consider upgrading to the latest stable Expo SDK"
            ))

    return findings


# ===== Template C: Farm =====

def farm_checks(frontend_files, root):
    """Run all Farm-specific performance checks."""
    findings = []

    # 3C.1 Bundle Size and Tree Shaking
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        # Check for barrel imports that prevent tree shaking
        barrel_pattern = re.compile(r"import\s*\{[^}]{100,}\}\s*from")
        for match in barrel_pattern.finditer(content):
            line = content[:match.start()].count('\n') + 1
            findings.append(new_finding(
                "rendering_performance", "B",
                "Large destructured import may prevent tree shaking",
                fpath, line,
                "Import with many named exports from a single module",
                "May prevent effective tree shaking under Farm's bundler",
                "Import only what you need or use direct path imports"
            ))

    # 3C.2 Code Splitting
    findings.extend(_check_code_splitting(frontend_files))

    # 3C.3 Re-render Patterns
    findings.extend(_check_rerender_patterns(frontend_files))

    # 3C.4 Static Asset Optimization
    # Check for non-optimized image formats in public/static dirs
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in {'.png', '.jpg', '.jpeg', '.bmp'}:
                fpath = os.path.join(dirpath, fname)
                size = os.path.getsize(fpath)
                if size > 200_000:  # > 200KB
                    findings.append(new_finding(
                        "rendering_performance", "C",
                        f"Large unoptimized image: {fname}",
                        fpath, 0,
                        f"Image file is {size // 1024}KB — not in WebP/AVIF format",
                        "Large images slow page load, especially on mobile connections",
                        "Convert to WebP or AVIF format for 30-80% size reduction"
                    ))
        if len(findings) > 5:
            break

    # 3C.5 Memory Leaks
    findings.extend(_check_memory_leaks(frontend_files))

    return findings

# ---------------------------------------------------------------------------
# Shared Frontend Check Helpers
# ---------------------------------------------------------------------------

def _check_rerender_patterns(frontend_files, check_flatlist=False):
    """Check for unnecessary re-render patterns."""
    findings = []
    # Inline arrow functions in JSX props
    inline_fn_pattern = re.compile(r'(?:on\w+|render\w+)\s*=\s*\{\s*\(\s*\)\s*=>')

    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        for match in inline_fn_pattern.finditer(content):
            line = content[:match.start()].count('\n') + 1
            severity = "B" if check_flatlist and 'renderItem' in content[max(0,match.start()-100):match.start()+100] else "C"
            findings.append(new_finding(
                "rendering_performance", severity,
                "Inline function in JSX prop",
                fpath, line,
                "Arrow function defined directly in JSX prop — creates new function reference every render",
                "Triggers re-renders of child components on every parent render",
                "Extract to a named function or use `useCallback()` hook"
            ))
            if len(findings) > 8:
                return findings

    return findings


def _check_memory_leaks(frontend_files, severity="C"):
    """Check for missing useEffect cleanup."""
    findings = []
    # Pattern: useEffect with setInterval/setTimeout/addEventListener but no return cleanup
    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        # Find useEffect blocks
        effect_pattern = re.compile(r'useEffect\s*\(\s*\(\s*\)\s*=>\s*\{')
        for match in effect_pattern.finditer(content):
            # Get the effect body (up to ~500 chars)
            start = match.end()
            body = content[start:start+500]

            has_timer = any(kw in body for kw in ['setInterval', 'setTimeout', 'addEventListener'])
            has_cleanup = 'return' in body and ('clear' in body or 'remove' in body)

            if has_timer and not has_cleanup:
                line = content[:match.start()].count('\n') + 1
                findings.append(new_finding(
                    "rendering_performance", severity,
                    "useEffect missing cleanup",
                    fpath, line,
                    "useEffect sets up timer/listener but doesn't return a cleanup function",
                    "Causes memory leaks — timers and listeners accumulate on re-renders and navigation",
                    "Add `return () => { clearInterval(id); }` or `return () => { removeEventListener(...); }` cleanup"
                ))

    return findings


def _check_list_virtualization(frontend_files):
    """Check for large list rendering without virtualization."""
    findings = []
    virtualization_libs = {'react-window', 'react-virtual', 'react-virtualized', 'FlatList'}

    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        has_virtualization = any(lib in content for lib in virtualization_libs)
        if not has_virtualization:
            # Check for .map() that could be large lists
            map_pattern = re.compile(r'\.map\(\s*\(\s*\w+')
            for match in map_pattern.finditer(content):
                # Heuristic: if the variable name suggests a list (items, data, results, etc)
                context = content[max(0, match.start()-50):match.start()]
                if any(kw in context.lower() for kw in ['items', 'data', 'results', 'list', 'rows', 'records']):
                    line = content[:match.start()].count('\n') + 1
                    findings.append(new_finding(
                        "rendering_performance", "C",
                        "List rendering without virtualization",
                        fpath, line,
                        "`.map()` renders potentially large list without virtualization",
                        "All items render at once — causes slow initial render and high memory for large datasets",
                        "Use `react-window` or `react-virtual` for lists that could grow beyond ~50 items"
                    ))
                    break  # One per file

    return findings


def _check_code_splitting(frontend_files):
    """Check for heavy static imports that should be dynamic."""
    findings = []
    heavy_pattern = re.compile(
        r"import\s+.*from\s+['\"](?:chart\.js|recharts|@nivo|leaflet|mapbox|monaco-editor|three|pdf-lib|quill|draft-js)['\"]"
    )

    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue
        for match in heavy_pattern.finditer(content):
            if 'dynamic(' not in content and 'lazy(' not in content:
                line = content[:match.start()].count('\n') + 1
                findings.append(new_finding(
                    "rendering_performance", "B",
                    "Heavy library not code-split",
                    fpath, line,
                    f"Static import of heavy library without code splitting",
                    "Increases initial bundle — all users download it even if feature is rarely used",
                    "Use dynamic import: `const Chart = dynamic(() => import('...'), { ssr: false })`"
                ))

    return findings

# ---------------------------------------------------------------------------
# Step 4: Cross-Cutting Checks (All Templates)
# ---------------------------------------------------------------------------

def check_algorithmic_complexity(backend_files, frontend_files):
    """4.1 — Flag nested iterations over same dataset."""
    findings = []
    all_files = backend_files + frontend_files

    for fpath in all_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            # Detect nested .find/.filter/.some/.includes on arrays
            nested_patterns = [
                r'\.filter\(.*\.filter\(',
                r'\.find\(.*\.find\(',
                r'\.some\(.*\.some\(',
                r'\.includes\(.*\.includes\(',
                r'for\s.*for\s.*\.includes\(',
            ]
            for pattern in nested_patterns:
                if re.search(pattern, line):
                    findings.append(new_finding(
                        "code_algorithms", "B",
                        "Nested iteration pattern",
                        fpath, i,
                        "Nested array operations (filter inside filter, find inside find) detected",
                        "O(n²) complexity — performance degrades quadratically with data size",
                        "Use a Map/Set for lookups or restructure to single-pass algorithm"
                    ))
                    break

    return findings


def check_inefficient_data_structures(backend_files, frontend_files):
    """4.2 — Flag array.includes/find inside loops where Set/Map would be better."""
    findings = []
    all_files = backend_files + frontend_files

    for fpath in all_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        lines = content.split('\n')
        in_loop = False

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in ['for ', 'for(', '.forEach(', '.map(', '.filter(']):
                in_loop = True
            elif in_loop and not stripped:
                in_loop = False

            if in_loop and ('.includes(' in line or '.indexOf(' in line or '.find(' in line):
                findings.append(new_finding(
                    "code_algorithms", "C",
                    "Array lookup inside loop",
                    fpath, i,
                    "`.includes()`, `.indexOf()`, or `.find()` called on array inside a loop",
                    "O(n) lookup on every iteration — total O(n×m) complexity",
                    "Convert the lookup array to a `Set` before the loop for O(1) lookups"
                ))

    return findings[:10]


def check_missing_parallelization(backend_files, frontend_files):
    """4.3 — Flag consecutive await/Promise calls that could be parallelized."""
    findings = []

    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        # Look for consecutive fetch/axios calls
        lines = content.split('\n')
        consecutive_fetches = []
        for i, line in enumerate(lines, 1):
            if 'await fetch(' in line or 'await axios' in line:
                consecutive_fetches.append(i)
            else:
                if len(consecutive_fetches) >= 2:
                    findings.append(new_finding(
                        "code_algorithms", "C",
                        "Sequential API calls",
                        fpath, consecutive_fetches[0],
                        f"{len(consecutive_fetches)} consecutive await fetch/axios calls",
                        "Sequential calls wait for each to complete — total time is sum of all",
                        "Use `Promise.all([fetch(...), fetch(...)])` to run independent calls in parallel"
                    ))
                consecutive_fetches = []

    return findings

# ---------------------------------------------------------------------------
# Step 5: Mobile & Responsive Checks (web templates only, skip Expo)
# ---------------------------------------------------------------------------

HTML_ENTRY_FILES = {"index.html", "_document.js", "_document.tsx", "document.js",
                     "document.tsx", "app.html", "layout.tsx", "layout.js"}


def check_meta_viewport(frontend_files, all_files, root):
    """5.1 — Check for <meta name="viewport"> in HTML entry points."""
    findings = []
    candidates = []
    for fpath in all_files:
        fname = os.path.basename(fpath)
        if fname in HTML_ENTRY_FILES:
            candidates.append(fpath)
    for subpath in ["public/index.html", "frontend/public/index.html",
                    "src/index.html", "frontend/index.html",
                    "pages/_document.tsx", "pages/_document.js",
                    "frontend/pages/_document.tsx", "frontend/pages/_document.js",
                    "app/layout.tsx", "app/layout.js",
                    "frontend/app/layout.tsx", "frontend/app/layout.js"]:
        fpath = os.path.join(root, subpath)
        if os.path.exists(fpath) and fpath not in candidates:
            candidates.append(fpath)

    if not candidates:
        return findings

    found_viewport = False
    incomplete_viewport = None
    for fpath in candidates:
        content = read_file_safe(fpath)
        if not content:
            continue
        if re.search(r'<meta\s+name=["\']viewport["\']', content, re.IGNORECASE):
            found_viewport = True
            viewport_match = re.search(
                r'<meta\s+name=["\']viewport["\']\s+content=["\']([^"\']*)["\']',
                content, re.IGNORECASE
            )
            if viewport_match:
                vp_content = viewport_match.group(1)
                if 'width=device-width' not in vp_content:
                    line = content[:viewport_match.start()].count('\n') + 1
                    incomplete_viewport = (fpath, line, vp_content)
            break

    if not found_viewport:
        findings.append(new_finding(
            "mobile_responsive", "A",
            "Missing meta viewport tag",
            candidates[0], 0,
            "No `<meta name=\"viewport\">` tag found in HTML entry point",
            "Mobile browsers render at desktop width and shrink — text unreadable, tap targets tiny",
            'Add `<meta name="viewport" content="width=device-width, initial-scale=1">` to the HTML <head>'
        ))
    elif incomplete_viewport:
        fpath, line, vp_content = incomplete_viewport
        findings.append(new_finding(
            "mobile_responsive", "B",
            "Incomplete viewport meta tag",
            fpath, line,
            f"Viewport meta tag has `content=\"{vp_content}\"` — missing `width=device-width`",
            "Page may not scale correctly on mobile devices",
            'Update to `content="width=device-width, initial-scale=1"`'
        ))

    return findings


def check_media_query_coverage(frontend_files):
    """5.2 — Check CSS/JSX/TSX for mobile-width media queries."""
    findings = []
    has_any_media = False
    has_mobile_breakpoint = False
    files_with_styles = 0

    mobile_bp_pattern = re.compile(
        r'@media[^{]*max-width\s*:\s*(\d+)\s*px'
        r'|@media[^{]*min-width\s*:\s*(\d+)\s*px'
    )
    any_media_pattern = re.compile(r'@media\s*[\(\[]')

    for fpath in frontend_files:
        ext = os.path.splitext(fpath)[1].lower()
        if ext not in {'.css', '.scss', '.less', '.jsx', '.tsx', '.js', '.ts'}:
            continue
        content = read_file_safe(fpath)
        if not content:
            continue
        if 'style' in content.lower() or ext in {'.css', '.scss', '.less'}:
            files_with_styles += 1

        if any_media_pattern.search(content):
            has_any_media = True

        for match in mobile_bp_pattern.finditer(content):
            max_w = match.group(1)
            min_w = match.group(2)
            if max_w and int(max_w) <= 768:
                has_mobile_breakpoint = True
            if min_w and int(min_w) <= 480:
                has_mobile_breakpoint = True

    if files_with_styles > 0 and not has_any_media:
        findings.append(new_finding(
            "mobile_responsive", "B",
            "No responsive media queries found",
            "project-wide", 0,
            f"Scanned {files_with_styles} style-containing files — no `@media` rules detected",
            "Layout is fixed-width — will overflow or appear broken on mobile screens",
            "Add responsive breakpoints: `@media (max-width: 768px) { ... }` for tablet and `@media (max-width: 480px) { ... }` for mobile"
        ))
    elif has_any_media and not has_mobile_breakpoint:
        findings.append(new_finding(
            "mobile_responsive", "B",
            "No mobile-width breakpoint",
            "project-wide", 0,
            "Media queries found but none target mobile widths (<=768px)",
            "Layout may adapt for tablets but break on phone-sized screens",
            "Add a mobile breakpoint: `@media (max-width: 480px) { ... }` for phone layouts"
        ))

    return findings


def check_touch_targets(frontend_files):
    """5.3 — Flag buttons/clickable elements with explicit small sizes in JSX/TSX."""
    findings = []
    small_size_pattern = re.compile(
        r'(?:width|height|minWidth|minHeight)\s*[:=]\s*["\']?(\d+)(?:px)?["\']?'
    )
    interactive_context = re.compile(
        r'<(?:button|Button|TouchableOpacity|TouchableHighlight|Pressable|a |Link )[^>]*'
    )

    for fpath in frontend_files:
        ext = os.path.splitext(fpath)[1].lower()
        if ext not in {'.jsx', '.tsx'}:
            continue
        content = read_file_safe(fpath)
        if not content:
            continue

        for match in interactive_context.finditer(content):
            element_str = content[match.start():match.start() + 500]
            close = element_str.find('>')
            if close > 0:
                element_str = element_str[:close]

            for size_match in small_size_pattern.finditer(element_str):
                val = int(size_match.group(1))
                if 0 < val < 44:
                    line = content[:match.start()].count('\n') + 1
                    findings.append(new_finding(
                        "mobile_responsive", "B",
                        f"Small touch target ({val}px)",
                        fpath, line,
                        f"Interactive element has explicit size {val}px — below 44px minimum for touch targets",
                        "Users on mobile will struggle to tap small buttons — causes mis-taps and frustration",
                        "Set minimum touch target size to 44x44px (Apple HIG) or 48x48px (Material Design)"
                    ))
                    break  # One per element

        if len(findings) >= 10:
            break

    return findings


def check_viewport_units(frontend_files):
    """5.4 — Flag 100vh usage (problematic on mobile browsers)."""
    findings = []
    vh_pattern = re.compile(r'(?:height|min-height|max-height)\s*:\s*100vh')

    for fpath in frontend_files:
        content = read_file_safe(fpath)
        if not content:
            continue

        for match in vh_pattern.finditer(content):
            line = content[:match.start()].count('\n') + 1
            findings.append(new_finding(
                "mobile_responsive", "C",
                "100vh usage (mobile address bar issue)",
                fpath, line,
                "`height: 100vh` includes the area behind the mobile browser address bar",
                "Content is clipped or requires scrolling on mobile — the address bar takes ~56px that 100vh doesn't account for",
                "Replace with `height: 100dvh` (dynamic viewport height), or use `min-height: 100vh` with `min-height: -webkit-fill-available` fallback"
            ))

        if len(findings) >= 5:
            break

    return findings[:5]


# ---------------------------------------------------------------------------
# Step 6: Scoring
# ---------------------------------------------------------------------------

def calculate_scores(findings):
    """Calculate per-category and weighted final scores."""
    category_findings = defaultdict(lambda: {"A": 0, "B": 0, "C": 0, "D": 0})

    for f in findings:
        cat = f["category"]
        sev = f["severity"]
        category_findings[cat][sev] += 1

    def category_score(counts):
        return max(0, 100 - (counts["A"] * 30) - (counts["B"] * 15) - (counts["C"] * 5) - (counts["D"] * 1))

    backend = category_findings.get("backend_efficiency", {"A": 0, "B": 0, "C": 0, "D": 0})
    rendering = category_findings.get("rendering_performance", {"A": 0, "B": 0, "C": 0, "D": 0})
    algorithms = category_findings.get("code_algorithms", {"A": 0, "B": 0, "C": 0, "D": 0})
    mobile = category_findings.get("mobile_responsive", {"A": 0, "B": 0, "C": 0, "D": 0})

    backend_score = category_score(backend)
    rendering_score = category_score(rendering)
    algorithms_score = category_score(algorithms)
    mobile_score = category_score(mobile)

    weighted = (backend_score * 0.30) + (rendering_score * 0.30) + (algorithms_score * 0.25) + (mobile_score * 0.15)

    # Count total A/B/C/D
    total_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for cat_counts in category_findings.values():
        for sev, count in cat_counts.items():
            total_counts[sev] += count

    # Determine grade
    if weighted >= 85 and total_counts["A"] == 0:
        grade = "A"
    elif weighted >= 70 and total_counts["A"] == 0 and total_counts["B"] <= 3:
        grade = "B"
    elif weighted >= 50 and total_counts["A"] <= 2:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": round(weighted, 1),
        "grade": grade,
        "category_scores": {
            "backend_efficiency": {"score": backend_score, "weight": 0.30},
            "rendering_performance": {"score": rendering_score, "weight": 0.30},
            "code_algorithms": {"score": algorithms_score, "weight": 0.25},
            "mobile_responsive": {"score": mobile_score, "weight": 0.15},
        },
        "finding_counts": total_counts,
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    root = PROJECT_ROOT

    # Step 1: Template Detection
    template = detect_template(root)

    # Collect files
    backend_files, frontend_files, all_files = collect_files(root)

    # Step 2: Universal Backend Checks
    findings = []
    findings.extend(check_async_handlers(backend_files))
    findings.extend(check_n_plus_1(backend_files))
    findings.extend(check_unbounded_queries(backend_files))
    findings.extend(check_mongo_singleton(backend_files))
    findings.extend(check_missing_indexes(backend_files))
    findings.extend(check_sequential_async(backend_files))
    findings.extend(check_blocking_handlers(backend_files))
    findings.extend(check_over_fetching(backend_files))
    findings.extend(check_pydantic_overhead(backend_files))

    # Step 3: Template-Specific Checks
    if template == "nextjs":
        findings.extend(nextjs_checks(frontend_files, root))
    elif template == "expo":
        findings.extend(expo_checks(frontend_files, root))
    elif template == "farm":
        findings.extend(farm_checks(frontend_files, root))
    else:
        # Generic: run all template checks
        findings.extend(nextjs_checks(frontend_files, root))
        findings.extend(expo_checks(frontend_files, root))
        findings.extend(farm_checks(frontend_files, root))

    # Step 4: Cross-Cutting Checks
    findings.extend(check_algorithmic_complexity(backend_files, frontend_files))
    findings.extend(check_inefficient_data_structures(backend_files, frontend_files))
    findings.extend(check_missing_parallelization(backend_files, frontend_files))

    # Step 5: Mobile & Responsive Checks (skip for Expo — native app)
    if template != "expo":
        findings.extend(check_meta_viewport(frontend_files, all_files, root))
        findings.extend(check_media_query_coverage(frontend_files))
        findings.extend(check_touch_targets(frontend_files))
        findings.extend(check_viewport_units(frontend_files))

    # Step 6: Scoring
    scores = calculate_scores(findings)

    # Output JSON (same pattern as audit.py)
    report = {
        "template": template,
        "score": scores["score"],
        "grade": scores["grade"],
        "category_scores": scores["category_scores"],
        "finding_counts": scores["finding_counts"],
        "findings": findings,
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()


