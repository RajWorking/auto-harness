"""Submit a job to the agent optimization service, poll until it finishes, and print a summary.

    python test_client.py --max-iterations 3 --tasks crack-7z-hash pypi-server
"""

import argparse
import sys
import time

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--ref", default="main", help="branch, tag, or commit in the service's agent repo")
    parser.add_argument("--entrypoint", default="agent:HarnessAgent")
    parser.add_argument("--tasks", nargs="+", help="task IDs; default: the service's full task set")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--target-score", type=float, default=1.0, help="stop once this fraction of tasks passes")
    parser.add_argument("--poll-sec", type=float, default=10)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.url, timeout=180)
    body = {
        "agent": {"ref": args.ref, "entrypoint": args.entrypoint},
        "max_iterations": args.max_iterations,
        "target_score": args.target_score,
    }
    if args.tasks:
        body["task_ids"] = args.tasks
    resp = client.post("/jobs", json=body)
    if resp.status_code != 202:
        sys.exit(f"submit failed: {resp.status_code} {resp.text}")
    job = resp.json()
    print(f"job {job['id']} queued at commit {job['base_commit'][:12]}")

    while job["status"] in ("queued", "running"):
        time.sleep(args.poll_sec)
        job = client.get(f"/jobs/{job['id']}").raise_for_status().json()
        print(f"  {time.strftime('%H:%M:%S')} {job['status']}")

    iterations = client.get(f"/jobs/{job['id']}/iterations").raise_for_status().json()
    print_summary(job, iterations)
    if job["status"] != "completed":
        sys.exit(1)


def print_summary(job: dict, iterations: list[dict]) -> None:
    print(f"\nstatus: {job['status']}")
    if job["error"]:
        print(f"error: {job['error']}")
    print(f"stop reason: {job['stop_reason']}")

    print("\niterations:")
    for it in iterations:
        parent = f"on {it['parent'][:12]}" if it["parent"] else "base"
        print(f"  {it['index']}  {it['commit'][:12]}  {parent:<15}  score {it['result']['score']:.2f}")
        if it["analysis"]:
            print(f"     analysis: {it['analysis']}")

    result = job["latest_result"]
    if result is None:
        return
    print(f"\nlatest commit {job['latest_commit'][:12]}, score {result['score']:.2f}")
    for task in result["tasks"]:
        print(f"  {task['status']:<6}  {task['task_id']}")
    failures = [t for t in result["tasks"] if t["status"] != "passed"]
    if failures:
        print("\nfailures (last line of output):")
        for task in failures:
            lines = (task["failure"] or "").strip().splitlines()
            print(f"  {task['task_id']}: {lines[-1] if lines else 'no output'}")


if __name__ == "__main__":
    main()
