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

import aiohttp
import logging
from typing import List, Optional

from .common import trim_slash
from .client import configurable_retry
from . import config

from .models import RunCodeRequest, RunCodeResponse, EvalResult, \
    GetPromptByIdRequest, GetPromptsRequest, Prompt, SubmitRequest, \
    RunJupyterRequest, RunJupyterResponse, RunStatus

logger = logging.getLogger(__name__)


async def run_code(request: RunCodeRequest,
                   endpoint: str = '',
                   max_attempts: int = 5,
                   client_timeout: Optional[float] = None) -> RunCodeResponse:

    @configurable_retry(max_attempts)
    async def _run_code(request: RunCodeRequest) -> RunCodeResponse:
        timeout = aiohttp.ClientTimeout(total=client_timeout) if client_timeout else None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f'{trim_slash(endpoint or config.SANDBOX_ENDPOINT)}/run_code',
                                    json=request.dict()) as result:
                if result.status != 200:
                    raise Exception(f'Faas api responded with code {result.status}: {await result.text()}')
                resp = RunCodeResponse(**(await result.json()))
                if resp.status == RunStatus.SandboxError:
                    raise Exception(f'Sandbox responded with error: {resp.message}')
                return resp

    return await _run_code(request)


async def run_jupyter(request: RunJupyterRequest,
                      endpoint: str = '',
                      max_attempts: int = 3,
                      client_timeout: Optional[float] = None) -> RunJupyterResponse:

    @configurable_retry(max_attempts)
    async def _run_jupyter(request: RunJupyterRequest) -> RunJupyterResponse:
        timeout = aiohttp.ClientTimeout(total=client_timeout) if client_timeout else None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f'{trim_slash(endpoint or config.SANDBOX_ENDPOINT)}/run_jupyter',
                                    json=request.dict()) as result:
                if result.status != 200:
                    raise Exception(f'Faas api responded with code {result.status}: {await result.text()}')
                resp = RunJupyterResponse(**(await result.json()))
                if resp.status == RunStatus.SandboxError:
                    raise Exception(f'Sandbox responded with error: {resp.message}')
                return resp

    return await _run_jupyter(request)


async def get_prompts(request: GetPromptsRequest, endpoint: str = '') -> List[Prompt]:
    async with aiohttp.ClientSession() as session:
        async with session.post(f'{trim_slash(endpoint or config.DATASET_ENDPOINT)}/get_prompts',
                                json=request.dict()) as result:
            if result.status != 200:
                raise Exception(f'Faas api responded with code {result.status}: {await result.text()}')
            resp = [Prompt(**r) for r in await result.json()]
            return resp


async def get_prompt_by_id(request: GetPromptByIdRequest, endpoint: str = '') -> Prompt:
    async with aiohttp.ClientSession() as session:
        async with session.post(f'{trim_slash(endpoint or config.DATASET_ENDPOINT)}/get_prompt_by_id',
                                json=request.dict()) as result:
            if result.status != 200:
                raise Exception(f'Faas api responded with code {result.status}: {await result.text()}')
            resp = Prompt(**(await result.json()))
            return resp


async def submit(request: SubmitRequest,
                 endpoint: str = '',
                 max_attempts: int = 5,
                 client_timeout: Optional[float] = None) -> EvalResult:

    @configurable_retry(max_attempts)
    async def _submit(request: SubmitRequest) -> EvalResult:
        timeout = aiohttp.ClientTimeout(total=client_timeout) if client_timeout else None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f'{trim_slash(endpoint or config.DATASET_ENDPOINT)}/submit',
                                    json=request.dict()) as result:
                if result.status != 200:
                    raise Exception(f'Faas api responded with code {result.status}: {await result.text()}')
                resp = EvalResult(**(await result.json()))
                return resp

    return await _submit(request)


async def submit_safe(request: SubmitRequest,
                      endpoint: str = '',
                      max_attempts: int = 5,
                      client_timeout: Optional[float] = None) -> EvalResult:
    try:
        return await submit(request, endpoint, max_attempts, client_timeout)
    except Exception:
        logger.warning('failed to request sandbox, a rejected result is returned')
        return EvalResult(id=request.id, accepted=False, extracted_code='', tests=[])
