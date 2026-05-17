---
sidebar_position: 3
---

# Python SDK

安装（需要Python >= 3.8）：

```bash
pip install sandbox-fusion
```

## 配置API端点

SDK默认使用环境变量 `SANDBOX_FUSION_ENDPOINT` 中的值作为API端点。如果该环境变量未设置，则默认使用 `http://localhost:8000` 。

您可以通过以下方式设置或更改API端点：

1. 设置环境变量：

```bash
export SANDBOX_FUSION_ENDPOINT="http://your-api-endpoint.com"
```

2. 使用 `set_endpoint` 函数：

```python
from sandbox_fusion import set_endpoint

set_endpoint("http://your-api-endpoint.com")
```

3. 在调用各个函数时指定 `endpoint` 参数：

```python
from sandbox_fusion import get_prompt_by_id, GetPromptByIdRequest

get_prompt_by_id(GetPromptByIdRequest(...), endpoint="http://your-api-endpoint.com")
```

注意：如果在函数调用时指定了 `endpoint` 参数，它将覆盖全局设置的endpoint。

## 函数和对应的数据结构

所有 HTTP API 都有一个对应的函数，接收一个 API 同名的数据结构。 具体的 API 语义请参考 HTTP API 文档。

### run_code

运行代码的函数。

```python
from sandbox_fusion import run_code, RunCodeRequest

# 使用默认重试次数（5次）
run_code(RunCodeRequest(code='print(123)', language='python'))

# 手动指定重试次数
run_code(RunCodeRequest(code='print(123)', language='python'), max_attempts=10)
```

- 请求数据结构：`RunCodeRequest`
- 响应数据结构：`RunCodeResponse`
- 默认重试次数：5

### run_jupyter

运行Jupyter notebook的函数。

```python
from sandbox_fusion import run_jupyter, RunJupyterRequest

run_jupyter(RunJupyterRequest(...))
```

- 请求数据结构：`RunJupyterRequest`
- 响应数据结构：`RunJupyterResponse`
- 默认重试次数：3

### get_prompts

获取提示词列表的函数。

```python
from sandbox_fusion import get_prompts, GetPromptsRequest

get_prompts(GetPromptsRequest(...))
```

- 请求数据结构：`GetPromptsRequest`
- 响应数据结构：`List[Prompt]`
- 无重试机制

### get_prompt_by_id

根据ID获取特定提示词的函数。

```python
from sandbox_fusion import get_prompt_by_id, GetPromptByIdRequest

get_prompt_by_id(GetPromptByIdRequest(...))
```

- 请求数据结构：`GetPromptByIdRequest`
- 响应数据结构：`Prompt`
- 无重试机制

### submit

提交评估请求的函数。

```python
from sandbox_fusion import submit, SubmitRequest

submit(SubmitRequest(...))
```

- 请求数据结构：`SubmitRequest`
- 响应数据结构：`EvalResult`
- 默认重试次数：5

### submit_safe

安全提交评估请求的函数。如果请求失败，会返回一个被拒绝的结果，而不是抛出异常。

```python
from sandbox_fusion import submit_safe, SubmitRequest

submit_safe(SubmitRequest(...))
```

- 请求数据结构：`SubmitRequest`
- 响应数据结构：`EvalResult`
- 默认重试次数：5

## 异步接口

所有现有接口都实现了异步版本，在对应的函数后面添加 _async 即可导入。

## 并发请求

沙盒提供了并发请求的工具，可以方便地批量执行某个函数操作。

下面是一个并发运行代码的例子：

```python
from sandbox_fusion import set_sandbox_endpoint, run_concurrent, run_code, RunCodeRequest
set_sandbox_endpoint('https://faas-code-sandbox.bytedance.net/')

codes = [f'print({i})' for i in range(123, 456)]
results = run_concurrent(run_code, args=[[RunCodeRequest(code=c, language='python')] for c in codes])
```

参考上文的接口即可替换成其它函数。

## 超时设置

客户端对较为耗时的函数了增加了超时设置能力，同步和异步版本都可用。 这些函数包括：

- `submit`
- `run_code`
- `run_jupyter`

，参数为 `client_timeout` ，一个例子：

```python
from sandbox_fusion import run_code, RunCodeRequest

print(
    run_code(RunCodeRequest(code='import time; time.sleep(4); print(123)', language='python'),
             max_attempts=1,
             client_timeout=3))
```
