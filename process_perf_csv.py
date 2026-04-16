"""
Performance audit pipeline orchestrator.
Same architecture as process_csv.py for code quality evals.

Phase 1 (no MCP needed — run locally):
  python3 process_perf_csv.py prep [csv_path] [start] [end]
  → Checks pods, wakes sleeping ones, outputs ready slugs to /tmp/perf_audit_queue.json

Phase 2 (called by Claude per slug — writes results back):
  python3 process_perf_csv.py write [csv_path] [slug] [check_report_file] [fix_prompt_file]

Phase 3 (mark skipped):
  python3 process_perf_csv.py skip [csv_path] [slug] [reason]
"""
import csv, json, os, sys, time, urllib.request

AUTH_TOKEN = os.environ.get("EMERGENT_AUTH_TOKEN", "")
if not AUTH_TOKEN:
    print("ERROR: Set EMERGENT_AUTH_TOKEN environment variable (Emergent JWT token)")
    print("  Get it from: browser DevTools → Network tab → any api.emergent.sh request → Authorization header")
    sys.exit(1)

QUEUE_PATH = "/tmp/perf_audit_queue.json"


def read_csv(path):
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def check_pod(job_id):
    try:
        req = urllib.request.Request(
            f"https://api.emergent.sh/trajectories/v0/stream?job_id={job_id}&last_request_id=5b6feb4e-b686-4e22-82f5-87aeee44fb32",
            headers={
                "accept": "text/event-stream",
                "authorization": f"Bearer {AUTH_TOKEN}",
                "cache-control": "no-cache",
                "content-type": "application/json",
                "origin": "https://app.emergent.sh",
            },
        )
        resp = urllib.request.urlopen(req)
        data = resp.read().decode()
        return "awake" if data.strip() else "sleeping"
    except Exception:
        return "sleeping"


def wake_pod(job_id, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"https://api.emergent.sh/jobs/v0/{job_id}/restart-environment?upgrade=false&source=manual_wakeup",
                data=b"",
                headers={
                    "accept": "*/*",
                    "authorization": f"Bearer {AUTH_TOKEN}",
                    "content-type": "application/json",
                    "origin": "https://app.emergent.sh",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode()).get("status") == "success"
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
            continue
    return False


def cmd_prep(csv_path, start, end):
    """Check pods, wake sleeping ones, output ready queue."""
    rows, fieldnames = read_csv(csv_path)
    queue = []
    skipped_awake = 0
    skipped_done = 0
    waking = []

    for idx, row in enumerate(rows):
        if idx < start or idx >= end:
            continue
        if row.get("Perf eval", "").strip():
            skipped_done += 1
            continue

        job_id = row["latest_job_id"]
        slug = row["slug"]
        status = check_pod(job_id)

        if status == "awake":
            row["Perf eval"] = "skipped: pod already awake"
            skipped_awake += 1
            print(f"  [{idx+1}] {slug}: AWAKE — skipped")
        else:
            print(f"  [{idx+1}] {slug}: sleeping — waking...")
            time.sleep(1)
            if wake_pod(job_id):
                waking.append({"idx": idx, "slug": slug, "job_id": job_id})
            else:
                row["Perf eval"] = "error: failed to wake pod"
                print(f"  [{idx+1}] {slug}: WAKE FAILED")

    write_csv(csv_path, rows, fieldnames)

    if waking:
        print(f"\nWaiting 15s for {len(waking)} pods to start...")
        time.sleep(15)

    with open(QUEUE_PATH, "w") as f:
        json.dump(waking, f)

    print(f"\nReady: {len(waking)} | Skipped (awake): {skipped_awake} | Skipped (done): {skipped_done}")
    print(f"Queue written to {QUEUE_PATH}")


def cmd_write_inline(csv_path, slug, check_report, fix_prompt):
    """Write perf audit results inline."""
    rows, fieldnames = read_csv(csv_path)
    for row in rows:
        if row["slug"] == slug:
            row["Perf eval"] = check_report
            row["Fix prompt"] = fix_prompt
            break
    write_csv(csv_path, rows, fieldnames)
    print(f"Written: {slug}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prep"
    csv_path = sys.argv[2] if len(sys.argv) > 2 else "perf_audit_input.csv"

    if cmd == "prep":
        start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        end = int(sys.argv[4]) if len(sys.argv) > 4 else 999999
        cmd_prep(csv_path, start, end)
    elif cmd == "write":
        slug = sys.argv[3]
        check_report = sys.argv[4]
        fix_prompt = sys.argv[5]
        cmd_write_inline(csv_path, slug, check_report, fix_prompt)
    elif cmd == "skip":
        slug = sys.argv[3]
        reason = sys.argv[4] if len(sys.argv) > 4 else "skipped"
        cmd_write_inline(csv_path, slug, reason, "")
