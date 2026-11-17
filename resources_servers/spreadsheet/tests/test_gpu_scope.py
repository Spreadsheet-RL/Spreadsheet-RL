import sys
from types import SimpleNamespace

import pytest

from resources_servers.spreadsheet.packaging import gpu_scope, smoke_driver


@pytest.mark.parametrize("text", ["", "0,0", "-1,2", "0, 1", "0,"])
def test_invalid_indices_are_rejected(text):
    with pytest.raises(ValueError):
        gpu_scope.parse_indices(text)


def test_selected_gpus_are_pinned_and_other_gpu_processes_are_ignored(monkeypatch):
    monkeypatch.setattr(gpu_scope.socket, "gethostname", lambda: "train-node")
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        if "-i" in command:
            return "\n".join(f"{i}, GPU-{i}, 4, 0" for i in range(6))
        return "GPU-6, 123, 80000\nGPU-7, 456, 80000\n"

    monkeypatch.setattr(gpu_scope.subprocess, "check_output", query)
    scope = gpu_scope.inspect_selected(gpu_scope.parse_indices("0,1,2,3,4,5"), "train-node")
    assert scope["uuids"] == [f"GPU-{i}" for i in range(6)]
    assert calls[0][2] == "0,1,2,3,4,5"
    with pytest.raises(ValueError, match="UUID mapping changed"):
        gpu_scope.inspect_selected(list(range(6)), "train-node", ["GPU-replaced"] * 6)


@pytest.mark.parametrize("memory,utilization,process", [(2048, 0, ""), (4, 90, ""), (4, 0, "GPU-0, 123, 4\n")])
def test_busy_selected_gpus_are_rejected_without_stopping_processes(monkeypatch, memory, utilization, process):
    monkeypatch.setattr(gpu_scope.socket, "gethostname", lambda: "train-node")

    def query(command, **kwargs):
        assert command[0] == "nvidia-smi"
        if "-i" in command:
            return f"0, GPU-0, {memory}, {utilization}\n"
        return process

    monkeypatch.setattr(gpu_scope.subprocess, "check_output", query)
    with pytest.raises(ValueError, match="no processes were stopped"):
        gpu_scope.inspect_selected([0], "train-node")


def test_wrong_hostname_is_rejected_before_querying_gpus(monkeypatch):
    monkeypatch.setattr(gpu_scope.socket, "gethostname", lambda: "other-node")
    monkeypatch.setattr(
        gpu_scope.subprocess, "check_output", lambda *args, **kwargs: pytest.fail("wrong node queried")
    )
    with pytest.raises(ValueError, match="Wrong training instance"):
        gpu_scope.inspect_selected([0], "train-node")


@pytest.mark.parametrize("valid_cluster", [True, False])
def test_driver_pins_visibility_and_rejects_wrong_ray_pool_before_actors(monkeypatch, valid_cluster):
    uuids = [f"GPU-{i}" for i in range(6)]
    expected_tag = "nrl_tag_" + "_".join(uuids)
    events = []
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "prior-value")

    def inspect(indices, hostname, expected):
        assert indices == list(range(6)) and hostname == "train-node" and expected == uuids
        events.append("gpu_check")
        return {"uuids": uuids}

    monkeypatch.setattr(gpu_scope, "inspect_selected", inspect)

    def init_ray(**kwargs):
        assert smoke_driver.os.environ["CUDA_VISIBLE_DEVICES"] == ",".join(uuids)
        assert kwargs == {"log_dir": "/tmp/short-ray"}
        events.append("init_ray")

    cluster = SimpleNamespace(init_ray=init_ray)
    distributed = SimpleNamespace(virtual_cluster=cluster)
    ray = SimpleNamespace(
        cluster_resources=lambda: {"GPU": 6, expected_tag: 1} if valid_cluster else {"GPU": 8},
        shutdown=lambda: events.append("detach"),
    )
    monkeypatch.setitem(sys.modules, "nemo_rl", SimpleNamespace(distributed=distributed))
    monkeypatch.setitem(sys.modules, "nemo_rl.distributed", distributed)
    monkeypatch.setitem(sys.modules, "ray", ray)

    def trainer(path, run_name):
        assert path == "/fixture/train.py" and run_name == "__main__"
        cluster.init_ray()
        events.append("allocate_actors")

    monkeypatch.setattr(smoke_driver.runpy, "run_path", trainer)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "driver",
            "--entrypoint",
            "/fixture/train.py",
            "--config",
            "/fixture/full.yaml",
            "--ray-root",
            "/tmp/short-ray",
            "--gpu-indices",
            "0,1,2,3,4,5",
            "--gpu-uuids",
            ",".join(uuids),
            "--expected-hostname",
            "train-node",
        ],
    )
    if valid_cluster:
        smoke_driver.main()
        assert events == ["gpu_check", "init_ray", "allocate_actors"]
    else:
        with pytest.raises(ValueError, match="Ray cluster does not match"):
            smoke_driver.main()
        assert events == ["gpu_check", "init_ray", "detach"]
    assert cluster.init_ray is init_ray


@pytest.mark.parametrize("resources", [{"GPU": 6}, {"GPU": 8, "nrl_tag_GPU-0": 1}])
def test_wrong_ray_resource_count_or_uuid_tag_is_rejected(resources):
    with pytest.raises(ValueError):
        gpu_scope.validate_ray_scope(resources, ["GPU-0"])
