"""Read-only checks for the explicitly selected GPUs; never stop other jobs."""

import csv
import io
import socket
import subprocess


def parse_indices(text):
    if not text or any(not part.isdecimal() for part in text.split(",")):
        raise ValueError("GPU indices must be comma-separated nonnegative integers")
    indices = [int(part) for part in text.split(",")]
    if len(indices) != len(set(indices)):
        raise ValueError("GPU indices must be unique")
    return indices


def inspect_selected(indices, expected_hostname, expected_uuids=None):
    if socket.gethostname() != expected_hostname:
        raise ValueError("Wrong training instance for the selected GPUs")
    result = subprocess.check_output(
        [
            "nvidia-smi",
            "-i",
            ",".join(map(str, indices)),
            "--query-gpu=index,uuid,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    rows = {
        int(row[0]): {
            "index": int(row[0]),
            "uuid": row[1].strip(),
            "memory_mb": int(row[2]),
            "utilization": int(row[3]),
        }
        for row in csv.reader(io.StringIO(result))
        if row
    }
    if set(rows) != set(indices):
        raise ValueError("Selected GPU indices are not all visible on this instance")
    ordered = [rows[index] for index in indices]
    uuids = [row["uuid"] for row in ordered]
    if expected_uuids is not None and uuids != list(expected_uuids):
        raise ValueError("GPU UUID mapping changed since preparation")
    if any(row["memory_mb"] > 1024 or row["utilization"] > 5 for row in ordered):
        raise ValueError("A selected GPU is no longer idle; no processes were stopped")
    processes = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    if any(row and row[0].strip() in uuids for row in csv.reader(io.StringIO(processes))):
        raise ValueError("A selected GPU has a compute process; no processes were stopped")
    return {"hostname": expected_hostname, "indices": indices, "uuids": uuids, "devices": ordered}


def validate_ray_scope(resources, uuids):
    tag = "nrl_tag_" + "_".join(sorted(uuids))
    if resources.get("GPU") != len(uuids) or not resources.get(tag):
        raise ValueError("Ray cluster does not match the selected GPU UUIDs; refusing to allocate training actors")
