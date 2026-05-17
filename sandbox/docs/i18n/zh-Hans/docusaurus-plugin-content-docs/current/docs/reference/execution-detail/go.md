# Go

版本： 1.21.6

Go 支持两种运行模式： `go_test` 和 `go` 。

无论哪种模式，沙盒都会先将[内置的 Go 项目模板](https://github.com/bytedance/SandboxFusion/tree/main/runtime/go) 复制到临时文件夹，并写入代码。

对于 `go_test` 模式，执行单条指令 `go test <filename>` 。

对于 `go` 模式，编译指令为 `go build -o out <filename>` ，执行指令为 `./out`
