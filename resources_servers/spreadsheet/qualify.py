"""Check a copy of each oracle against its original cached values using LibreOffice.

Passing this gate checks the oracle, not every formula a future policy may write.
Keep an Excel evaluation track when comparing with Spreadsheet-RL paper results.
"""

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from resources_servers.spreadsheet.service.catalog import sha256_file, under
from resources_servers.spreadsheet.service.engines import LibreOfficePool
from resources_servers.spreadsheet.service.workbook import compare


def qualify(manifest, data_root, output, report, engine):
    if output.exists() or report.exists():
        raise FileExistsError("Qualification output already exists")
    accepted, diagnostics = [], []
    with tempfile.TemporaryDirectory(prefix="spreadsheet-qualification-") as directory:
        for line in manifest.read_text().splitlines():
            item = json.loads(line)
            oracle = under(data_root, item["oracle"])
            if sha256_file(oracle) != item["oracle_sha256"]:
                raise ValueError("Oracle checksum mismatch")
            result = {"task_id": item["task_id"], "qualified": False}
            try:
                # Two independent recalculations catch common volatile/unstable oracles.
                for index in range(2):
                    copy = Path(directory) / f"oracle_{index}.xlsx"
                    shutil.copyfile(oracle, copy)
                    engine.recalculate(copy)
                    score = compare(oracle, copy, item["answer_position"])
                    if score["reward"] != 1:
                        result.update(reason="oracle_value_mismatch", **score)
                        break
                else:
                    item.update(libreoffice_qualified=True, qualified_engine=engine.version)
                    accepted.append(item)
                    result.update(qualified=True)
            except Exception:
                result["reason"] = "recalculation_or_comparison_failed"
            diagnostics.append(result)
    output.write_text("".join(json.dumps(row) + "\n" for row in accepted))
    report.write_text(
        json.dumps(
            {
                "engine": engine.version,
                "source_manifest_sha256": sha256_file(manifest),
                "qualified": len(accepted),
                "total": len(diagnostics),
                "tasks": diagnostics,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "data-root", "output", "report", "worker-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--uno-python", default="/usr/bin/python3")
    parser.add_argument("--soffice", default="/usr/bin/soffice")
    args = parser.parse_args()
    engine = LibreOfficePool(args.worker_root, args.uno_python, args.soffice)
    try:
        qualify(args.manifest, args.data_root, args.output, args.report, engine)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
