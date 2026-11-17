"""Recheck seed preservation after correcting audit-only formula serialization.

The full audit is reused only if the entire service and schema trees are exactly
unchanged. Strict values, oracle hashes, native controls and all baseline IDs
remain evidence from that immutable full run; only seed gates run again here.
"""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import modal

HERE = (
    Path(__file__).resolve().parent
    if modal.is_local()
    else Path("/source/resources_servers/spreadsheet/packaging")
)
sys.path.insert(0, str(HERE.parents[2]))
from resources_servers.spreadsheet.packaging.modal_audit_dataset import (  # noqa: E402
    PYTHON,
    SOURCE,
    VOLUME,
    image,
    subprocess_run,
)

app = modal.App("spreadsheet-seed-gate-recheck")


@app.function(
    image=image, volumes={"/artifacts": VOLUME}, cpu=2, memory=8192, timeout=300
)
def preflight(run_id):
    root = Path("/artifacts/audit-runs") / run_id
    root.mkdir(parents=True, exist_ok=True)
    subprocess_run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-c",
            "/dev/null",
            "-q",
            "/source/resources_servers/spreadsheet/tests/test_rejection_audit.py",
            "/source/resources_servers/spreadsheet/tests/test_ooxml_metadata.py",
            "/source/resources_servers/spreadsheet/tests/test_answer_metadata.py",
            "/source/resources_servers/spreadsheet/tests/test_reward_readers.py",
        ],
        root / "seed-gate-tests.log",
        timeout=240,
    )
    VOLUME.commit()
    return (root / "seed-gate-tests.log").read_text()


@app.function(
    image=image,
    volumes={"/artifacts": VOLUME},
    cpu=2,
    memory=8192,
    timeout=900,
    max_containers=8,
    block_network=True,
)
def recheck_seed(task):
    import subprocess

    code = r"""
import hashlib,json,sys,tempfile
from pathlib import Path
from zipfile import ZipFile
sys.path.insert(0,'/source')
from resources_servers.spreadsheet.packaging.recovery_cohort import seed_roundtrip
from resources_servers.spreadsheet.service.engines import LibreOfficePool
job=json.loads(sys.argv[1])
with ZipFile('/artifacts/audit-inputs/6f274cd4cbc27e656d7ddae7c3b591669866dc84/spreadsheets.zip') as archive:
 seed=archive.read(job['thread']+'/output.xlsx')
 oracle=archive.read(job['thread']+'/target.xlsx')
 if hashlib.sha256(seed).hexdigest()!=job['seed_sha256'] or hashlib.sha256(oracle).hexdigest()!=job['oracle_sha256']:
  raise ValueError('Pinned seed/oracle hash differs')
with tempfile.TemporaryDirectory() as folder:
 root=Path(folder);path=root/'seed.xlsx';path.write_bytes(seed)
 engine=LibreOfficePool(root/'office',workers=1,timeout=120)
 try:result=seed_roundtrip(path,engine)
 finally:engine.close()
print(json.dumps({'task_id':job['task_id'],'seed_roundtrip':result}))
"""
    result = subprocess.run(
        [PYTHON, "-I", "-c", code, json.dumps(task)],
        capture_output=True,
        text=True,
        timeout=800,
    )
    if result.returncode:
        raise RuntimeError("Seed gate subprocess failed; " + result.stderr[-1000:])
    return json.loads(result.stdout)


@app.function(
    image=image, volumes={"/artifacts": VOLUME}, cpu=1, memory=2048, timeout=120
)
def persist(run_id, records, summary):
    root = Path("/artifacts/audit-runs") / run_id
    root.mkdir(exist_ok=True)
    if (root / "summary.json").exists():
        raise ValueError("Refusing to replace a completed gate audit")
    (root / "candidate.audit.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    VOLUME.commit()


@app.local_entrypoint()
def main(source_commit: str, full_audit_commit: str, source_run: str, output_dir: str):
    import re
    import subprocess
    from uuid import uuid4

    if subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
    ).strip() != source_commit or subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=SOURCE, text=True
    ):
        raise ValueError("Exact clean source commit required")
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            full_audit_commit,
            source_commit,
            "--",
            "resources_servers/spreadsheet/service",
            "resources_servers/spreadsheet/schemas.py",
        ],
        cwd=SOURCE,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", source_run):
        raise ValueError("Unsafe source run ID")
    prefix = f"audit-runs/{source_run}/"
    original_summary = b"".join(VOLUME.read_file(prefix + "summary.json"))
    original_candidates = b"".join(VOLUME.read_file(prefix + "candidate.audit.jsonl"))
    summary = json.loads(original_summary)
    records = [json.loads(line) for line in original_candidates.splitlines()]
    if (
        summary["identity"]["source_commit"] != full_audit_commit
        or summary["previous_baseline_regressions"]
        or len(records) != 125
    ):
        raise ValueError("Complete regression-free pinned full audit required")
    run_id = "seed-gates-" + source_commit[:12] + "-" + uuid4().hex[:8]
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "run-started.json").write_text(
        json.dumps({"run_id": run_id, "app_id": app.app_id}) + "\n"
    )
    (output / "seed-gate-tests.log").write_text(preflight.remote(run_id))
    selected = [r for r in records if r["strict_qualified"]]
    inputs = [
        {key: r[key] for key in ("task_id", "thread", "seed_sha256", "oracle_sha256")}
        for r in selected
    ]
    results = list(recheck_seed.map(inputs, order_outputs=False))
    new_gates = {r["task_id"]: r["seed_roundtrip"] for r in results}
    verdicts = Counter(summary["verdicts"])
    for record in records:
        if record["task_id"] in new_gates:
            verdicts.subtract([record["verdict"]])
            record["initial_seed_roundtrip"] = record["seed_roundtrip"]
            record["seed_roundtrip"] = new_gates[record["task_id"]]
            record["verdict"] = (
                "recovered_strict"
                if all(
                    record.get(gate, {}).get("passed")
                    for gate in (
                        "seed_roundtrip",
                        "native_roundtrip",
                        "negative_control",
                    )
                )
                else "recovered_scoring_only"
            )
            verdicts.update([record["verdict"]])
    groups = {}
    for name in summary["candidate_groups"]:
        rows = [r for r in records if r["candidate_group"] == name]
        groups[name] = {
            "checked": len(rows),
            "verdicts": dict(Counter(r["verdict"] for r in rows)),
            "strict_values_passed": sum(r["strict_qualified"] for r in rows),
        }
        for gate, field in [
            ("native_roundtrip", "native_passed"),
            ("seed_roundtrip", "seed_passed"),
            ("negative_control", "negative_controls_passed"),
        ]:
            groups[name][field] = sum(
                r.get(gate, {}).get("passed", False) for r in rows
            )
    recovered = [r for r in records if r["verdict"] == "recovered_strict"]
    summary.update(
        candidate_groups=groups,
        strict_recovered=len(recovered),
        verdicts=dict(+verdicts),
        recovered_by_split=dict(Counter(r["split"] for r in recovered)),
        seed_gate_recheck={
            "run_id": run_id,
            "source_commit": source_commit,
            "full_audit_commit": full_audit_commit,
            "service_and_schema_trees_identical": True,
            "source_run": source_run,
            "original_summary_sha256": hashlib.sha256(original_summary).hexdigest(),
            "original_candidates_sha256": hashlib.sha256(
                original_candidates
            ).hexdigest(),
            "checked": len(selected),
            "workers_max": 8,
            "app_id": app.app_id,
        },
    )
    persist.remote(run_id, records, summary)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "candidate.audit.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )
    print(
        "SEED_GATE_RESULT="
        + json.dumps(
            {
                "strict_recovered": len(recovered),
                "candidate_groups": groups,
                "run_id": run_id,
            }
        )
    )
