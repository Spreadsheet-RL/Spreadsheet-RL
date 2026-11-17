"""Invoke the existing trainer with its Ray log_dir hook set to a short path."""

import argparse
import functools
import os
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ray-root", required=True)
    parser.add_argument("--gpu-indices")
    parser.add_argument("--gpu-uuids")
    parser.add_argument("--expected-hostname")
    args = parser.parse_args()

    uuids = None
    if args.gpu_indices:
        if not args.gpu_uuids or not args.expected_hostname:
            parser.error("GPU selection requires UUIDs and the expected hostname")
        if __package__:
            from .gpu_scope import inspect_selected, parse_indices
        else:
            from gpu_scope import inspect_selected, parse_indices
        scope = inspect_selected(parse_indices(args.gpu_indices), args.expected_hostname, args.gpu_uuids.split(","))
        uuids = scope["uuids"]
        # Set visibility before importing NeMo-RL/Torch/Ray.
        os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(uuids)

    from nemo_rl.distributed import virtual_cluster

    # Keep cluster startup in NeMo-RL's helper, then verify its GPU selection
    # before allowing the entrypoint to allocate training/rollout actors.
    original = virtual_cluster.init_ray

    def selected_init_ray():
        import ray

        if __package__:
            from .gpu_scope import validate_ray_scope
        else:
            from gpu_scope import validate_ray_scope
        result = original(log_dir=args.ray_root)
        try:
            validate_ray_scope(ray.cluster_resources(), uuids)
        except Exception:
            ray.shutdown()  # Detach this driver; never stop an existing cluster.
            raise
        return result

    virtual_cluster.init_ray = selected_init_ray if uuids else functools.partial(original, log_dir=args.ray_root)
    sys.argv = [args.entrypoint, "--config", args.config]
    try:
        runpy.run_path(args.entrypoint, run_name="__main__")
    finally:
        virtual_cluster.init_ray = original


if __name__ == "__main__":
    main()
