# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Optional

import aiohttp
from fastapi import HTTPException
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from sandbox.configs.run_config import RunConfig
from sandbox.datasets.types import CommandRunStatus, RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.runners.types import cpu_languages, gpu_languages
from sandbox.server.sandbox_api import run_code

config = RunConfig.get_instance_sync()

MAJOR_LANGUAGES = [
    'python', 'cpp', 'nodejs', 'go', 'go_test', 'java', 'csharp', 'typescript', 'rust', 'php', 'bash', 'jest'
]
MINOR_LANGUAGES = ['lua', 'R', 'perl', 'D_ut', 'ruby', 'scala', 'julia', 'kotlin_script', 'verilog']

logger = logging.getLogger(__name__)


def on_retry_error(s):
    e = s.outcome.exception()
    logger.error(f'give up requesting sandbox. error: {e}. request: {s.args[0].model_dump_json(indent=2)}')
    raise HTTPException(status_code=500, detail=f'failed to request sandbox: {e}')


def before_retry_sleep(s):
    logger.warning(
        f'error requesting sandbox for {s.attempt_number} time(s), will retry... error: {s.outcome.exception()}. request: {s.args[0].model_dump_json(indent=2)}'
    )


async def post_run_request(request: RunCodeRequest, endpoint: str) -> RunCodeResponse:
    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=request.model_dump(), timeout=320) as response:
            if response.status != 200:
                raise Exception(f'Faas api responded with code {response.status}: {await response.text()}')
            result = await response.json()
    return RunCodeResponse(**result)


@retry(wait=wait_exponential_jitter(),
       stop=stop_after_attempt(1),
       before_sleep=before_retry_sleep,
       retry_error_callback=on_retry_error)
async def run_code_in_sandbox(request: RunCodeRequest) -> RunCodeResponse:
    language = request.language
    if language in cpu_languages and config.dataset.cpu_runner_url:
        resp = await post_run_request(request, config.dataset.cpu_runner_url)
    elif language in gpu_languages and config.dataset.gpu_runner_url:
        resp = await post_run_request(request, config.dataset.gpu_runner_url)
    else:
        resp = await run_code(request)
    if resp.status == RunStatus.SandboxError:
        raise Exception(f'Sanbox responded with error: {resp.message}')
    return resp


@retry(wait=wait_exponential_jitter(),
       stop=stop_after_attempt(5),
       before_sleep=before_retry_sleep,
       retry_error_callback=on_retry_error)
async def run_code_in_sandbox_w_retry(request: RunCodeRequest) -> RunCodeResponse:
    language = request.language
    if language in cpu_languages and config.dataset.cpu_runner_url:
        resp = await post_run_request(request, config.dataset.cpu_runner_url)
    elif language in gpu_languages and config.dataset.gpu_runner_url:
        resp = await post_run_request(request, config.dataset.gpu_runner_url)
    else:
        resp = await run_code(request)
    if resp.status == RunStatus.SandboxError:
        raise Exception(f'Sanbox responded with error: {resp.message}')
    return resp


class SummaryMapping(BaseModel):
    Success: str = RunStatus.Success
    Failed: str = RunStatus.Failed
    CompileFailed: Optional[str] = None
    CompileTimeout: Optional[str] = None
    RunFailed: Optional[str] = None
    RunTimeout: Optional[str] = None


def summary_result(result: RunCodeResponse, mapping: SummaryMapping) -> str:
    if result.compile_result is None and result.run_result is None:
        # note: this should not happen
        if result.status == RunStatus.Success:
            return mapping.Success
        if result.status == RunStatus.Failed:
            return mapping.Failed
        raise Exception(f'unexpected result status {result.status}')
    if result.run_result is None:
        # compile error
        if result.compile_result.status == CommandRunStatus.TimeLimitExceeded:
            return mapping.CompileTimeout or mapping.Failed
        return_code = result.compile_result.return_code
        if return_code is None:
            raise Exception(f'invalid sandbox result: no return code with status {result.compile_result.status}')
        if return_code != 0:
            return mapping.CompileFailed or mapping.Failed
        raise Exception(f'invalid sandbox result: compiled succesfully with no run result')
    if result.run_result.status == CommandRunStatus.TimeLimitExceeded:
        return mapping.RunTimeout or mapping.Failed
    return_code = result.run_result.return_code
    if return_code is None:
        raise Exception(f'invalid sandbox result: no return code with status {result.run_result.status}')
    if return_code != 0:
        return mapping.RunFailed or mapping.Failed
    return mapping.Success
