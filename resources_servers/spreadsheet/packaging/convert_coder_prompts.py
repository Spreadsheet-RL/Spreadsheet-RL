"""Create a Coder-prompt variant of an existing qualified Spreadsheet Gym dataset."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path


FORMAT_PROMPT = """Spreadsheet tool-call format for this request:
When calling a tool, emit one call using exactly this format:

<tool_call>
<function=inspect_range>
<parameter=range>
A1:C3
</parameter>
</function>
</tool_call>

Use the actual tool name and its declared parameter names. Use one <parameter=name>
block per argument. Put string values directly in the block, without JSON quotes.
For arrays or objects, put the JSON value inside its parameter block.
End the turn after the closing </tool_call> and wait for the tool response.
Do not wrap a JSON object with name and arguments in <tool_call>. The format
above takes precedence over generic tool-call examples for this spreadsheet task.
"""


def load_lines(path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def convert_row(row):
    if row.get("agent_ref") != {"name": "spreadsheet_agent", "type": "responses_api_agents"}:
        raise ValueError("Expected a Spreadsheet Gym row")
    request = row["responses_create_params"]
    messages = request["input"]
    system = messages[0]
    if system["role"] != "system" or not isinstance(system["content"], str):
        raise ValueError("Expected a leading text system message")
    if FORMAT_PROMPT in system["content"]:
        raise ValueError("Input already contains the Coder prompt")
    if not any(tool.get("name") == "inspect_range" for tool in request["tools"]):
        raise ValueError("The example tool is missing from this row")
    updated = dict(system, content=system["content"].rstrip("\n") + "\n\n" + FORMAT_PROMPT)
    return dict(row, responses_create_params=dict(request, input=[updated, *messages[1:]]))


def convert(source_dir, manifest, output_dir):
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(output_dir)
    qualified = {}
    for entry in load_lines(manifest):
        task_id = entry["task_id"]
        if task_id in qualified or entry["libreoffice_qualified"] is not True:
            raise ValueError("Expected a unique qualified manifest entry for each task")
        qualified[task_id] = entry["dataset_revision"]
    manifest_hash = digest(manifest)
    receipt = {"prompt_variant": "qwen3_coder_v1", "manifest_sha256": manifest_hash, "splits": {}}
    seen = set()
    with tempfile.TemporaryDirectory(prefix=".coder-prompts-", dir=output_dir.parent) as folder:
        stage = Path(folder)
        for split in ("train", "test"):
            filename = split + ".jsonl"
            source = source_dir / filename
            target = stage / filename
            before = digest(source)
            rows = 0
            with target.open("x", encoding="utf-8") as stream:
                for row in load_lines(source):
                    task_id = row["task_id"]
                    if task_id in seen or qualified.get(task_id) != row["dataset_revision"]:
                        raise ValueError("Task ID or revision does not match the qualified dataset")
                    seen.add(task_id)
                    stream.write(json.dumps(convert_row(row), ensure_ascii=False) + "\n")
                    rows += 1
            if not rows or digest(source) != before:
                raise ValueError(f"{split} split is empty or changed during conversion")
            receipt["splits"][split] = {
                "rows": rows,
                "input_sha256": before,
                "output_sha256": digest(target),
            }
        if seen != qualified.keys() or digest(manifest) != manifest_hash:
            raise ValueError("Converted splits must cover the unchanged qualified manifest")
        report = stage / "conversion.json"
        report.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        output_dir.mkdir(mode=0o700)
        for artifact in stage.iterdir():
            if artifact != report:
                artifact.replace(output_dir / artifact.name)
        report.replace(output_dir / report.name)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = convert(args.input_dir, args.manifest, args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir), **result}, indent=2))


if __name__ == "__main__":
    main()
