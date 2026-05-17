# Lean

版本： 4.10.0-rc2

Lean 项目基于 `lake` 构建和运行，对应的项目位于 [这里](https://github.com/bytedance/SandboxFusion/tree/main/runtime/lean) 。

由于 Lean 的基础库 Mathlib 编译非常缓慢，而且完整的编译缓存体积很大，我们选择了 Mathlib 的一个子集进行编译缓存，这一部分子集足以用于 MiniF2F 的评估。 具体的子集位于[这里](https://github.com/bytedance/SandboxFusion/blob/main/runtime/lean/Main.lean)，我们参考了 https://github.com/yangky11/miniF2F-lean4 。

代码运行时，沙盒会将这个 lake 项目的完整结构复制到一个临时目录，将传入代码写入 `Main.lean` ，执行 `lake build` 运行。 需要注意的是， lean 对证明正确性的检查是在编译时期进行的，运行生成的二进制文件并没有特殊意义。 因此 Lean 语言没有 `compile_result` ，只有 `run_result` ，对应 `lake build` 指令的结果。
