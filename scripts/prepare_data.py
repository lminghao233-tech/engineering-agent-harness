"""Download pinned first-party Parquet, separate answers, freeze a stratified sample before execution."""
import argparse
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VISIBLE = ("instance_id", "repo", "base_commit", "problem_statement")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def download(client, url, maximum=40_000_000):
    content = bytearray()
    with client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > maximum:
                raise ValueError("download exceeds size limit")
    return bytes(content)


def stratify(rows, count, seed):
    groups = defaultdict(list)
    for row in rows:
        groups[row["repo"]].append(row["instance_id"])
    rng = random.Random(seed)
    for group in groups.values():
        group.sort()
        rng.shuffle(group)
    result = []
    keys = sorted(groups)
    while len(result) < min(count, len(rows)):
        for key in keys:
            if groups[key] and len(result) < count:
                result.append(groups[key].pop())
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", help="immutable HF commit; current revision resolved once on first download")
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--count", type=int, default=30)
    args = parser.parse_args()
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    manifest_path = data / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if args.revision and args.revision != existing["revision"]:
            raise SystemExit("Existing frozen manifest is immutable. Use a separate checkout for a different revision.")
        args.revision = existing["revision"]
        if args.seed != existing["seed"] or args.count != len(existing["selected_test_ids"]):
            raise SystemExit("Refusing to change a frozen sample")
    dataset = "SWE-bench/SWE-bench_Lite"
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        metadata = client.get(f"https://huggingface.co/api/datasets/{dataset}" + (f"/revision/{args.revision}" if args.revision else "")).raise_for_status().json()
        revision = metadata["sha"]
        sources, splits = [], defaultdict(list)
        for file in metadata["siblings"]:
            name = file["rfilename"]
            if not name.endswith(".parquet"):
                continue
            split = Path(name).name.split("-")[0]
            url = f"https://huggingface.co/datasets/{dataset}/resolve/{revision}/{name}"
            content = download(client, url)
            local = data / "downloads" / Path(name).name
            local.parent.mkdir(exist_ok=True)
            local.write_bytes(content)
            rows = pq.read_table(local).to_pylist()
            splits[split].extend(rows)
            sources.append({"url": url, "sha256": digest(content), "bytes": len(content), "split": split, "rows": len(rows), "fields": sorted(rows[0])})
        if "dev" not in splits or "test" not in splits:
            raise ValueError("expected official Lite dev and test splits; inspect upstream schema")
        test_ids = stratify(splits["test"], args.count, args.seed)
        dev_ids = [r["instance_id"] for r in sorted(splits["dev"], key=lambda r:r["instance_id"])[:3]]
        if set(test_ids) & set(dev_ids):
            raise ValueError("development / test overlap")
        visible, evaluator = [], []
        for split, rows in splits.items():
            seen = set()
            for row in rows:
                issue_identity = (row["repo"], row["instance_id"].rsplit("-", 1)[-1])
                if issue_identity in seen:
                    raise ValueError("duplicate issue identity")
                seen.add(issue_identity)
                if row["instance_id"] not in set(test_ids + dev_ids):
                    continue
                visible.append({**{k:row[k] for k in VISIBLE}, "split":split, "data_revision":revision})
                evaluator.append(row)
        for directory, content in (("agent-visible",visible),("evaluator-only",evaluator)):
            dest = data / directory
            dest.mkdir(exist_ok=True)
            (dest / "tasks.json").write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            if directory == "evaluator-only":
                try:
                    dest.chmod(0o700)
                    (dest / "tasks.json").chmod(0o600)
                except OSError:
                    pass  # Windows requires deployment ACLs, never mount this directory into tasks.
        readme_url = f"https://huggingface.co/datasets/{dataset}/raw/{revision}/README.md"
        card = download(client, readme_url)
        (data / "downloads" / "dataset-card.md").write_bytes(card)
        licenses = []
        projects = {r["repo"]:r["base_commit"] for r in visible}
        for repo, commit in sorted(projects.items()):
            found = None
            for name in ("LICENSE", "LICENSE.txt", "LICENSE.rst", "COPYING"):
                url = f"https://raw.githubusercontent.com/{repo}/{commit}/{name}"
                response = client.get(url)
                if response.status_code == 200:
                    text = response.content
                    dest = data / "licenses" / (repo.replace("/", "__") + ".txt")
                    dest.parent.mkdir(exist_ok=True)
                    dest.write_bytes(text)
                    found = {"repo":repo,"base_commit":commit,"url":url,"sha256":digest(text),"license_file":str(dest.relative_to(ROOT)),"scope":"retrieved file at representative selected baseline; review per repository"}
                    break
            licenses.append(found or {"repo":repo,"status":"manual_license_review_required"})
        manifest = {"dataset":dataset,"revision":revision,"retrieved_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"seed":args.seed,
                    "selection_rule":"sort repositories; seeded shuffle within repository; round-robin until 30; success independent; no replacements",
                    "split_counts":{k:len(v) for k,v in splits.items()},"selected_test_ids":test_ids,"development_ids":dev_ids,
                    "instances":[{k:r[k] for k in ("instance_id","repo","base_commit","split")} for r in visible],
                    "sources":sources,"card":{"url":readme_url,"sha256":digest(card),"declared_license":metadata.get("cardData",{}).get("license","not declared; do not infer from benchmark code")},
                    "upstream_licenses":licenses,"hints_enabled":False,"answers_exposed_to_agent":False,
                    "citation":"Jimenez et al., SWE-bench: Can Language Models Resolve Real-World GitHub Issues?, ICLR 2024, https://arxiv.org/abs/2310.06770",
                    "contamination":"Public benchmark may be present in model pretraining; cannot rule out contamination."}
        if manifest_path.exists():
            old = json.loads(manifest_path.read_text())
            if old["selected_test_ids"] != test_ids:
                raise ValueError("frozen sample mismatch")
        else:
            manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"revision":revision,"splits":manifest["split_counts"],"selected":len(test_ids),"development":dev_ids}))


if __name__ == "__main__":
    main()
