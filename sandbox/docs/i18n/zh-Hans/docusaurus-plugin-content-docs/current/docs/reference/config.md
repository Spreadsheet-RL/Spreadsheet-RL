---
sidebar_position: 3
---

# 配置文件

通过环境变量 SANDBOX_CONFIG 来控制运行时使用哪个配置文件。 对应的文件路径为 `sandbox/configs/{SANDBOX_CONFIG}.yaml` 。 默认情况下配置为 `sandbox/configs/local.yaml` 。

```yaml
online_judge:
  database:
    backend:
      type: mysql  # or none
      user: mysql_user
      password: mysql_password
      host: mysql_host
      port: mysql_port
    cache:  # 可选的缓存，删除则不使用
      path: memory  # sqlite数据库路径，文件地址或者memory
      sources:
      - type: mysql  # 缓存数据来源，mysql为从线上数据库获取，local为本地文件夹获取
        tables:
        - name: table_name
          columns: [id, labels, content, test]
  # 请求沙盒时的最大并发
  max_runner_concurrency: 5
runner:
  isolation: none  # 运行代码时的隔离等级，none（不隔离）或者lite（overlayfs+cgroups+namespace，建议在docker环境内使用以免宿主机受影响）
  cleanup_process: false  # 每次请求结束后杀死所有与沙盒服务无关的进程，本地慎用
  restore_bash: true  # 每次请求结束后检查bash二进制完整性并尝试恢复
  max_concurrency: 0  # 最大并发，0不限制
common:
  logging_color: false  # 日志输出是否带颜色
```

下面举例说明几个场景的配置文件。

local.yaml 本地开发用：

```yaml
online_judge:
  database:
    backend:
      type: none  # 不连接线上数据库
    cache:
      path: memory  # 本地缓存sqlite数据库的位置，这里是内存
      sources:
        - type: local  # 读取本地文件夹内的jsonl文件作为缓存
          path: sandbox/tests/datasets/samples
  max_runner_concurrency: 3
runner:
  isolation: none
  cleanup_process: false
  restore_bash: false
  max_concurrency: 34
common:
  logging_color: true
```

ci.yaml 集成测试环境使用：

集成测试环境全部使用本地信息

```yaml
online_judge:
  database:
    backend:
      type: none
    cache:
      path: memory
      sources:
        - type: local
          path: sandbox/tests/datasets/samples
  max_runner_concurrency: 3
runner:
  isolation: lite
  cleanup_process: false
  restore_bash: false
  max_concurrency: 0  # concurrency in ci is limited by pytest-xdist
common:
  logging_color: true
```
