"""Invoke the existing trainer with its Ray log_dir hook set to a short path."""

import argparse
import functools
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ray-root", required=True)
    args = parser.parse_args()

    from nemo_rl.distributed import virtual_cluster

    # Keep all cluster behavior in NeMo-RL's existing helper. The entrypoint
    # calls it without options; bind only its documented log_dir argument.
    original = virtual_cluster.init_ray
    virtual_cluster.init_ray = functools.partial(original, log_dir=args.ray_root)
    sys.argv = [args.entrypoint, "--config", args.config]
    try:
        runpy.run_path(args.entrypoint, run_name="__main__")
    finally:
        virtual_cluster.init_ray = original


if __name__ == "__main__":
    main()
