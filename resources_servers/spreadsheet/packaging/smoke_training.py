"""Run a two-step NeMo-RL spreadsheet smoke using the existing container runtime.

Writes only a fresh run directory and an explicitly declared short IPC root.
The training subprocess uses existing /opt actor venvs, offline local weights,
an inherited smoke YAML, and redacted logs. This is not a quality benchmark.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ACTORS = (
    "nemo_rl.algorithms.async_utils.AsyncTrajectoryCollector",
    "nemo_rl.algorithms.async_utils.ReplayBuffer",
    "nemo_rl.environments.nemo_gym.NemoGym",
    "nemo_rl.models.generation.vllm.vllm_worker_async.VllmAsyncGenerationWorker",
    "nemo_rl.models.policy.workers.megatron_policy_worker.MegatronPolicyWorker",
)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def model_metadata(model):
    config = json.loads((model / "config.json").read_text())
    tokenizer = json.loads((model / "tokenizer_config.json").read_text())
    template = tokenizer.get("chat_template", "")
    if not isinstance(template, str):
        raise ValueError("Review a non-string model chat template before running the smoke")
    if "<function=" in template:
        parser = "qwen3_coder"
    elif "<tool_call>" in template and '"arguments"' in template:
        parser = "hermes"
    else:
        raise ValueError("The model tool-call format is not covered by this smoke")
    special = {value["content"]: int(key) for key, value in tokenizer.get("added_tokens_decoder", {}).items()}
    eos = tokenizer.get("eos_token")
    eos = eos.get("content") if isinstance(eos, dict) else eos
    return {
        "model_type": config.get("model_type"),
        "tool_parser": parser,
        "eos": special.get(eos, config.get("eos_token_id")),
        "think_open": special.get("<think>"),
        "think_close": special.get("</think>"),
        "config_sha256": sha(model / "config.json"),
        "tokenizer_config_sha256": sha(model / "tokenizer_config.json"),
    }


def smoke_overrides(base, root, subset, metadata, six_gpu=False):
    from omegaconf import OmegaConf

    gym = OmegaConf.to_container(base.env.nemo_gym, resolve=False)
    # The inherited template defines unrelated judges even when their config
    # files are absent. Keep only the policy model servers used by this task.
    for name, value in list(gym.items()):
        if isinstance(value, dict) and any(
            key in value for key in ("resources_servers", "responses_api_agents", "responses_api_models", "_copy")
        ):
            if name not in ("policy_model", "policy_model_reasoning_off"):
                del gym[name]
    gym["nemo_gym_log_dir"] = str(root / "nemo_gym")
    gym["policy_model"]["responses_api_models"]["vllm_model"]["num_workers"] = 2
    if "policy_model_reasoning_off" in gym:
        gym["policy_model_reasoning_off"]["responses_api_models"]["vllm_model"]["num_workers"] = 1
    gym["spreadsheet_agent"] = {"responses_api_agents": {"spreadsheet_agent": {"max_steps": 6}}}
    overrides = {
        "checkpointing": {"enabled": False, "checkpoint_dir": str(root / "ckpt")},
        "grpo": {
            "num_prompts_per_step": 2,
            "num_generations_per_prompt": 2,
            "max_num_steps": 2,
            "val_at_start": False,
            "val_at_end": False,
            "val_period": -1,
        },
        "policy": {
            "generation": {
                "max_new_tokens": 4096,
                "vllm_cfg": {
                    "gpu_memory_utilization": 0.65,
                    "http_server_serving_chat_kwargs": {"tool_parser": metadata["tool_parser"]},
                },
                "vllm_kwargs": {"max_num_seqs": 8, "max_num_batched_tokens": 32768},
            },
            "megatron_cfg": {"sequence_parallel": False, "scheduler": {"lr_warmup_iters": 1}},
        },
        "data": {"shuffle": False, "train": [{"data_path": str(subset)}]},
        "env": {
            "_override_": True,
            "should_use_nemo_gym": True,
            "should_log_nemo_gym_responses": True,
            "should_log_hashed_nemo_gym_responses": False,
            "nemo_gym": gym,
        },
        "logger": {
            "log_dir": str(root / "logs"),
            "wandb_enabled": False,
            "tensorboard_enabled": True,
            "mlflow_enabled": False,
            "swanlab_enabled": False,
            "monitor_gpus": False,
        },
        "token_ids": {key: metadata[key] for key in ("eos", "think_open", "think_close") if metadata[key] is not None},
    }
    if six_gpu:
        overrides["cluster"] = {"num_nodes": 1, "gpus_per_node": 6}
        overrides["data"]["max_input_seq_length"] = 4096
        policy = overrides["policy"]
        policy.update(
            precision="bfloat16",
            train_global_batch_size=4,
            train_micro_batch_size=1,
            logprob_batch_size=1,
            logprob_chunk_size=4,
            max_total_sequence_length=8192,
        )
        policy["megatron_cfg"].update(
            tensor_model_parallel_size=4,
            pipeline_model_parallel_size=1,
            context_parallel_size=1,
            sequence_parallel=True,
            activation_checkpointing=True,
        )
        policy["megatron_cfg"]["optimizer"] = {"optimizer_cpu_offload": False, "optimizer_offload_fraction": 0.0}
        generation = policy["generation"]
        generation["colocated"] = {"enabled": False, "resources": {"gpus_per_node": 2, "num_nodes": 1}}
        generation["vllm_cfg"].update(
            tensor_parallel_size=2, pipeline_parallel_size=1, expert_parallel_size=1, max_model_len=8192
        )
        generation["vllm_kwargs"]["max_num_batched_tokens"] = 16384
    return overrides


def clean_environment(root, ray_root, gym_root):
    prefixes = ("CUDA_", "NVIDIA_", "NCCL_", "NRL_", "NEMO_", "NVTE_", "TORCH_", "OMP_", "MKL_")
    keep = {
        "PATH",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
        "LIBRARY_PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TZ",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if (key in keep or key.startswith(prefixes)) and not re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", key)
    }
    paths = {
        "NRL_MEGATRON_CHECKPOINT_DIR": "cache/nemo_rl",
        "VLLM_CACHE_ROOT": "cache/vllm",
        "HF_HOME": "cache/huggingface",
        "HF_DATASETS_CACHE": "cache/huggingface/datasets",
        "HUGGINGFACE_HUB_CACHE": "cache/huggingface/models",
        "HF_MODULES_CACHE": "cache/hf_modules",
        "TORCH_HOME": "cache/torch",
        "TORCH_EXTENSIONS_DIR": "cache/torch_extensions",
        "TORCHINDUCTOR_CACHE_DIR": "cache/torchinductor",
        "TRITON_CACHE_DIR": "cache/triton",
        "CUDA_CACHE_PATH": "cache/cuda",
        "XDG_CACHE_HOME": "cache/xdg",
        "UV_CACHE_DIR": "cache/uv",
    }
    for key, relative in paths.items():
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    # Include Ray's longest dashboard socket as well as its object store.
    if len(str(ray_root / "session_9999-99-99_99-99-99_999999_9999999/sockets/dash_MetricsHead").encode()) > 107:
        raise ValueError("Ray socket path is too long; select a shorter declared Ray root")
    temp = ray_root / "tmp"
    if len(str(temp / "99999999-9999-9999-9999-999999999999").encode()) > 107:
        raise ValueError("vLLM IPC socket path is too long; select a shorter IPC root")
    temp.mkdir(mode=0o700)
    env["TMPDIR"] = str(temp)
    env.update(
        RAY_TMPDIR=str(ray_root),
        PYTHONDONTWRITEBYTECODE="1",
        TOKENIZERS_PARALLELISM="false",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        UV_OFFLINE="1",
        PIP_NO_INDEX="1",
        NRL_FORCE_REBUILD_VENVS="false",
        NEMO_GYM_VENV_DIR=str(gym_root),
        NO_PROXY="*",
        no_proxy="*",
        CUDA_DEVICE_MAX_CONNECTIONS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        WANDB_MODE="disabled",
        NCCL_TIMEOUT="600",
        RAY_DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES=str(8 * 1024**3),
    )
    return env


def redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def gpu_snapshot():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        dict(zip(("index", "uuid", "memory_mb", "utilization"), (part.strip() for part in line.split(","))))
        for line in result.stdout.splitlines()
    ]


def redact_artifacts(roots, secrets):
    changed = 0
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".log", ".out", ".err", ".json", ".jsonl", ".yaml"}:
                continue
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_nlink != 1
                or path.stat().st_size > 16 * 1024**2
            ):
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            safe = redact(text, secrets)
            if safe != text:
                path.write_text(safe)
                changed += 1
    return changed


def existing_actor_root(trainer_root):
    selected = os.environ.get("NEMO_RL_VENV_DIR")
    candidates = [Path(selected)] if selected else [trainer_root / "venvs", Path("/opt/ray_venvs")]
    for candidate in candidates:
        if all((candidate / actor / "bin/python").is_file() for actor in ACTORS):
            return candidate.resolve()
    raise ValueError(
        "Missing prebuilt actor venvs; refusing to bootstrap. Checked: " + ", ".join(map(str, candidates))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("trainer-root", "base-config", "run-dir", "ipc-root", "private-config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--max-wall-seconds", type=int, default=1800)
    parser.add_argument("--gpu-indices", help="Qwen3-32B six-GPU smoke profile, e.g. 0,1,2,3,4,5")
    parser.add_argument("--helpers-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--reuse-cache-root", type=Path, help="Existing launcher directory containing cache/")
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong training instance")
    root, ipc_root = args.run_dir.resolve(), args.ipc_root.resolve()
    ray_root = ipc_root
    if root.exists() or ipc_root.exists():
        parser.error("Run and IPC directories must both be fresh")
    root.mkdir(mode=0o700)
    ipc_root.mkdir(mode=0o700)
    from nemo_rl.utils.config import load_config, register_omegaconf_resolvers
    from omegaconf import OmegaConf

    register_omegaconf_resolvers()
    base = load_config(args.base_config)
    if args.gpu_indices and Path(load_config.__code__.co_filename).resolve().parents[2] != args.trainer_root.resolve():
        raise ValueError("Activation selected a different trainer checkout")
    metadata = model_metadata(Path(base.policy.model_name))
    six_gpu = args.gpu_indices is not None
    scope = None
    if six_gpu:
        sys.path.insert(0, str(args.helpers_root))
        from gpu_scope import inspect_selected, parse_indices

        indices = parse_indices(args.gpu_indices)
        if indices != list(range(6)) or int(base.cluster.num_nodes) != 1 or int(base.cluster.gpus_per_node) != 6:
            raise ValueError("Require the assigned single-node GPU 0-5 layout")
        if Path(base.policy.model_name).name != "Qwen3-32B" or metadata["model_type"] != "qwen3":
            raise ValueError("This six-GPU profile requires the selected dense Qwen3-32B model")
        if not OmegaConf.select(base, "policy.megatron_cfg.enabled") or base.policy.generation.backend != "vllm":
            raise ValueError("The six-GPU profile requires the existing Megatron trainer and vLLM backend")
        if (
            base.get("on_policy_distillation", {}).get("enabled", False)
            or base.grpo.get("adv_estimator", {}).get("name") == "opd"
        ):
            raise ValueError("This is an RLVR smoke, not an OPD run")
        scope = inspect_selected(indices, args.expected_hostname)
        capacity = subprocess.check_output(
            ["nvidia-smi", "-i", args.gpu_indices, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
        )
        if len(capacity.splitlines()) != 6 or any(int(line.strip()) < 160000 for line in capacity.splitlines()):
            raise ValueError("The six-GPU profile requires the assigned large-memory GPUs")
    elif int(base.cluster.num_nodes) != 1 or int(base.cluster.gpus_per_node) != 2:
        raise ValueError("Legacy smoke requires two GPUs; select --gpu-indices for the six-GPU profile")
    actor_root = (
        existing_actor_root(args.trainer_root)
        if six_gpu
        else Path(os.environ.get("NEMO_RL_VENV_DIR", "/opt/ray_venvs"))
    )
    for actor in ACTORS:
        if not (actor_root / actor / "bin/python").is_file():
            raise ValueError(f"Missing prebuilt actor venv; refusing to bootstrap: {actor}")
    source = Path(base.data.train[0].data_path)
    with source.open() as stream:
        rows = [json.loads(next(stream)) for _ in range(4)]
    if any(row.get("agent_ref", {}).get("name") != "spreadsheet_agent" for row in rows):
        raise ValueError("Smoke rows must route to spreadsheet_agent")
    subset = root / "train.jsonl"
    subset.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    overrides = smoke_overrides(base, root, subset, metadata, six_gpu=six_gpu)
    from nemo_gym import PARENT_DIR as gym_source

    if six_gpu:
        if Path(gym_source).resolve() != (args.trainer_root / "3rdparty/Gym-workspace/Gym").resolve():
            raise ValueError("Activation selected a different Gym checkout")
        from prepare_training_node import probe_venv, service_probe
        import importlib.metadata as package_metadata

        expected_versions = {name: package_metadata.version(name) for name in ("ray", "openai")}
        cache = Path(base.env.nemo_gym.uv_venv_dir)
        for relative, module in (
            ("responses_api_models/vllm_model", None),
            ("resources_servers/spreadsheet", "resources_servers.spreadsheet.app"),
            ("responses_api_agents/spreadsheet_agent", "responses_api_agents.spreadsheet_agent.app"),
        ):
            result = probe_venv(cache / relative / ".venv", Path(gym_source), module)
            if (
                Path(result["prefix"]).resolve() != Path(result["venv_path"]).resolve()
                or result["versions"] != expected_versions
            ):
                raise ValueError("Existing cache interpreter/version mismatch")
        service_probe(args.private_config, "LibreOffice 24.2.7.2 420(Build:2)")

    audit_agent = args.helpers_root / "audit_agent.py"
    agent = overrides["env"]["nemo_gym"]["spreadsheet_agent"]["responses_api_agents"]["spreadsheet_agent"]
    agent.update(
        entrypoint=os.path.relpath(audit_agent, gym_source / "responses_api_agents/spreadsheet_agent"),
        num_workers=1,
        audit_path=str(root / "model_rollouts.jsonl"),
    )
    overrides = {"defaults": str(args.base_config.resolve()), **overrides}
    config = root / "smoke.yaml"
    config.write_text(OmegaConf.to_yaml(OmegaConf.create(overrides), resolve=False))
    resolved = OmegaConf.to_container(load_config(config), resolve=True)
    assert resolved["grpo"]["max_num_steps"] == 2
    assert resolved["policy"]["train_global_batch_size"] == 4
    if six_gpu:
        assert resolved["policy"]["megatron_cfg"]["tensor_model_parallel_size"] == 4
        assert resolved["policy"]["generation"]["vllm_cfg"]["tensor_parallel_size"] == 2
        # Freeze the effective smoke config, avoiding a later reread of the
        # user's YAML/defaults while this run is in flight.
        config.write_text(OmegaConf.to_yaml(OmegaConf.create(resolved), resolve=True))
        print(
            json.dumps(
                {
                    "event": "SIX_GPU_CONFIG_CHECKED",
                    "model": resolved["policy"]["model_name"],
                    "train_gpus": 4,
                    "train_tp": 4,
                    "train_dp": 1,
                    "rollout_gpus": 2,
                    "rollout_tp": 2,
                    "context": 8192,
                    "precision": "bfloat16",
                    "optimizer_cpu_offload": False,
                    "max_new_tokens": 4096,
                    "optimizer_steps": 2,
                    "prompts_per_step": 2,
                    "rollouts_per_prompt": 2,
                    "config": str(config),
                }
            ),
            flush=True,
        )
    env = clean_environment(
        args.reuse_cache_root.resolve() if args.reuse_cache_root else root,
        ray_root,
        Path(base.env.nemo_gym.uv_venv_dir),
    )
    before = scope["devices"] if scope else gpu_snapshot()
    if not six_gpu and (len(before) != 2 or any(int(gpu["memory_mb"]) > 1024 for gpu in before)):
        raise ValueError("The two allocated GPUs are no longer idle")
    env["CUDA_VISIBLE_DEVICES"] = ",".join(scope["uuids"] if scope else [gpu["index"] for gpu in before])
    env["NEMO_RL_VENV_DIR"] = str(actor_root)
    private = OmegaConf.load(args.private_config).spreadsheet_resources.resources_servers.spreadsheet
    secrets = [str(private.api_key), str(private.session_secret_key)]
    entry = args.trainer_root / "examples/nemo_gym/run_grpo_nemo_gym.py"
    driver = args.helpers_root / "smoke_driver.py"
    if not driver.is_file() or not audit_agent.is_file():
        raise ValueError("Pinned driver/audit helper is missing")
    report = {
        "status": "running",
        "hostname": socket.gethostname(),
        "model": metadata,
        "base_config": str(args.base_config),
        "base_config_sha256": sha(args.base_config),
        "config": str(config),
        "config_sha256": sha(config),
        "data_sha256": sha(subset),
        "source_data_sha256": sha(source),
        "task_ids": [row["task_id"] for row in rows],
        "entrypoint_sha256": sha(entry),
        "driver_sha256": sha(driver),
        "audit_agent_sha256": sha(audit_agent),
        "gpus_before": before,
        "actor_venv_root": str(actor_root),
        "gpu_profile": "train_tp4_rollout_tp2" if six_gpu else "legacy_two_gpu",
        "requested_optimizer_steps": 2,
        "max_wall_seconds": args.max_wall_seconds,
        "log": str(root / "train.log"),
        "ipc_root": str(ipc_root),
    }
    (root / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "event": "SPREADSHEET_TRAIN_SMOKE_START",
                "run_dir": str(root),
                "model": metadata,
                "steps": 2,
                "prompts_per_step": 2,
                "rollouts_per_prompt": 2,
            }
        ),
        flush=True,
    )
    start = time.monotonic()
    command = [
        sys.executable,
        "-B",
        str(driver),
        "--entrypoint",
        str(entry),
        "--config",
        str(config),
        "--ray-root",
        str(ray_root),
    ]
    if scope:
        command.extend(
            [
                "--gpu-indices",
                args.gpu_indices,
                "--gpu-uuids",
                ",".join(scope["uuids"]),
                "--expected-hostname",
                args.expected_hostname,
            ]
        )
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    report.update(driver_pid=process.pid, driver_pgid=process.pid)
    (root / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    timed_out = threading.Event()

    def stop_owned_run():
        if process.poll() is None:
            timed_out.set()
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    timer = threading.Timer(args.max_wall_seconds, stop_owned_run)
    timer.daemon = True
    timer.start()
    with (root / "train.log").open("w") as log:
        for raw in iter(process.stdout.readline, b""):
            line = redact(raw.decode("utf-8", errors="replace"), secrets)
            log.write(line)
            log.flush()
            if re.search(r"Traceback|ERROR|Exception|\bstep\b|reward|training|RL INIT|local Ray", line, re.IGNORECASE):
                print(line, end="", flush=True)
    code = process.wait()
    timer.cancel()
    report.update(
        status="exited" if code == 0 else "failed",
        exit_code=code,
        timed_out=timed_out.is_set(),
        elapsed_seconds=time.monotonic() - start,
        gpus_after=gpu_snapshot(),
        redacted_artifacts=redact_artifacts((root, ipc_root), secrets),
        base_config_unchanged=sha(args.base_config) == report["base_config_sha256"],
    )
    (root / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "event": "SPREADSHEET_TRAIN_SMOKE_EXIT",
                "exit_code": code,
                "timed_out": timed_out.is_set(),
                "receipt": str(root / "run.json"),
            }
        ),
        flush=True,
    )
    if six_gpu:
        if timed_out.is_set() and code == 0:
            code = 124
        if not report["base_config_unchanged"] and code == 0:
            code = 1
        try:
            sys.path.insert(0, str(args.helpers_root))
            from analyze_training_smoke import analyze

            analysis = analyze(root)
            (root / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
            summary = {
                key: analysis[key]
                for key in (
                    "logged_steps",
                    "trajectory_count",
                    "model_tool_calls",
                    "reward_counts",
                    "advantages",
                    "environment_error_count",
                    "problems",
                )
            }
            print(json.dumps({"event": "SIX_GPU_SMOKE_ANALYSIS", **summary}), flush=True)
            if code == 0 and analysis["problems"]:
                code = 1
        except Exception as error:
            print(json.dumps({"event": "SIX_GPU_ANALYSIS_FAILED", "error_type": type(error).__name__}), flush=True)
            if code == 0:
                code = 1
    raise SystemExit(code if code >= 0 else 128 - code)


if __name__ == "__main__":
    main()
