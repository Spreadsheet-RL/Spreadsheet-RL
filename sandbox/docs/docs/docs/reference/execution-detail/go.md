Here's the English translation:

# Go

Version: 1.21.6

Go supports two running modes: `go_test` and `go`.

For both modes, the sandbox first copies the [built-in Go project template](https://github.com/bytedance/SandboxFusion/tree/main/runtime/go) to a temporary folder and writes the code.

For `go_test` mode, it executes a single command `go test <filename>`.

For `go` mode, the compilation command is `go build -o out <filename>`, and the execution command is `./out`.
