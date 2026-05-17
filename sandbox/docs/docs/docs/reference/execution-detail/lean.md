# Lean

Version: 4.10.0-rc2

The Lean project is built and run using `lake`. The corresponding project is located [here](https://github.com/bytedance/SandboxFusion/tree/main/runtime/lean).

Since Lean's foundation library Mathlib is very slow to compile and the complete compilation cache is very large, we chose to compile a subset of Mathlib that is sufficient for MiniF2F evaluation. This subset can be found [here](https://github.com/bytedance/SandboxFusion/blob/main/runtime/lean/Main.lean), which we referenced from https://github.com/yangky11/miniF2F-lean4.

When running the code, the sandbox copies the complete structure of this lake project to a temporary directory, writes the input code to `Main.lean`, and executes `lake build`. It's important to note that Lean's proof correctness checking is done during compilation time, and running the generated binary file has no special significance. Therefore, the Lean language doesn't have a `compile_result`, only a `run_result`, which corresponds to the result of the `lake build` command.
