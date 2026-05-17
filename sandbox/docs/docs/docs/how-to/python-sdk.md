---
sidebar_position: 3
---

# Python SDK

Installation (requires Python >= 3.8):

```bash
pip install sandbox-fusion
```

## Configuring API Endpoint

By default, the SDK uses the value in the environment variable `SANDBOX_FUSION_ENDPOINT` as the API endpoint. If this environment variable is not set, it defaults to `http://localhost:8000`.

You can set or change the API endpoint in the following ways:

1. Set the environment variable:

```bash
export SANDBOX_FUSION_ENDPOINT="http://your-api-endpoint.com"
```

2. Use the `set_endpoint` function:

```python
from sandbox_fusion import set_endpoint

set_endpoint("http://your-api-endpoint.com")
```

3. Specify the `endpoint` parameter when calling individual functions:

```python
from sandbox_fusion import get_prompt_by_id, GetPromptByIdRequest

get_prompt_by_id(GetPromptByIdRequest(...), endpoint="http://your-api-endpoint.com")
```

Note: If the `endpoint` parameter is specified in a function call, it will override the globally set endpoint.

## APIs

All HTTP APIs have a corresponding function that accepts a data structure with the same name as the API. For specific API semantics, please refer to the HTTP API documentation.

### run_code

Function to run code.

```python
from sandbox_fusion import run_code, RunCodeRequest

# Using default retry count (5 times)
run_code(RunCodeRequest(code='print(123)', language='python'))

# Manually specify retry count
run_code(RunCodeRequest(code='print(123)', language='python'), max_attempts=10)
```

- Request data structure: `RunCodeRequest`
- Response data structure: `RunCodeResponse`
- Default retry count: 5

### run_jupyter

Function to run Jupyter notebook.

```python
from sandbox_fusion import run_jupyter, RunJupyterRequest

run_jupyter(RunJupyterRequest(...))
```

- Request data structure: `RunJupyterRequest`
- Response data structure: `RunJupyterResponse`
- Default retry count: 3

### get_prompts

Function to get the list of prompts.

```python
from sandbox_fusion import get_prompts, GetPromptsRequest

get_prompts(GetPromptsRequest(...))
```

- Request data structure: `GetPromptsRequest`
- Response data structure: `List[Prompt]`
- No retry mechanism

### get_prompt_by_id

Function to get a specific prompt by ID.

```python
from sandbox_fusion import get_prompt_by_id, GetPromptByIdRequest

get_prompt_by_id(GetPromptByIdRequest(...))
```

- Request data structure: `GetPromptByIdRequest`
- Response data structure: `Prompt`
- No retry mechanism

### submit

Function to submit an evaluation request.

```python
from sandbox_fusion import submit, SubmitRequest

submit(SubmitRequest(...))
```

- Request data structure: `SubmitRequest`
- Response data structure: `EvalResult`
- Default retry count: 5

### submit_safe

Function to safely submit an evaluation request. If the request fails, it will return a rejected result instead of throwing an exception.

```python
from sandbox_fusion import submit_safe, SubmitRequest

submit_safe(SubmitRequest(...))
```

- Request data structure: `SubmitRequest`
- Response data structure: `EvalResult`
- Default retry count: 5

## Asynchronous Interface

All existing interfaces have asynchronous versions implemented, which can be imported by adding _async to the corresponding function name.

## Concurrent Requests

The sandbox provides tools for concurrent requests, allowing convenient batch execution of certain function operations.

Here's an example of running code concurrently:

```python
from sandbox_fusion import set_sandbox_endpoint, run_concurrent, run_code, RunCodeRequest
set_sandbox_endpoint('https://faas-code-sandbox.bytedance.net/')

codes = [f'print({i})' for i in range(123, 456)]
results = run_concurrent(run_code, args=[[RunCodeRequest(code=c, language='python')] for c in codes])
```

You can replace with other functions by referring to the interfaces above.

## Timeout Settings

The package has added timeout configuration capabilities for time-consuming functions, available for both synchronous and asynchronous versions. These functions include:

- submit
- run_code
- run_jupyter

The parameter is `client_timeout`. Here's an example:

```python
from sandbox_fusion import run_code, RunCodeRequest

print(
    run_code(RunCodeRequest(code='import time; time.sleep(4); print(123)', language='python'),
             max_attempts=1,
             client_timeout=3))
```
