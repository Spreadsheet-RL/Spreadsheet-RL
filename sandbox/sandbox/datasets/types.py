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

from abc import ABC, abstractmethod
from string import Template
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from sandbox.configs.run_config import RunConfig
from sandbox.runners.types import CommandRunResult, CommandRunStatus, Language  # nopycln: import
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus  # nopycln: import

server_config = RunConfig.get_instance_sync()

# OJ related


class Message(BaseModel):
    role: str
    content: str


class Prompt(BaseModel):
    id: int | str
    prompt: str | List[Message]
    labels: Dict[str, Any] = {}


class TestConfig(BaseModel):
    '''
    custom_extract_logic: a piece of python code that calls `submit_code_blocks(cbs)` to extract custom code
                          cbs: List[CodeBlock], CodeBlock(priority=40, code='xxx', language='xxx')
                          priority: fenced = 30, incomplete fenced = 20, heuristic = 10
    '''
    __test__ = False
    dataset_type: Optional[str] = Field(
        None,
        examples=[None],
        description='the dataset class used to process, only works when the dataset id is not registered.')
    language: Optional[Language] = None
    locale: Optional[str] = None
    is_fewshot: Optional[bool] = None
    compile_timeout: Optional[float] = None
    run_timeout: Optional[float] = None
    custom_extract_logic: Optional[str] = None
    provided_data: Optional[List[Dict[str, Any]] | Dict[str, Any]] = None
    extra: Dict[str, Any] = {}


class GeneralStdioTest(BaseModel):
    # stdin / stdout for the standard streams, other names for files
    input: Dict[str, str]
    output: Dict[str, str]


class EvalTestCase(BaseModel):
    passed: bool
    exec_info: RunCodeResponse
    test_info: Optional[Dict[str, Any]] = None


class EvalResult(BaseModel):
    id: int | str
    accepted: bool
    extracted_code: str
    full_code: Optional[str] = None
    test_code: Optional[str] = None
    tests: List[EvalTestCase]
    extracted_type: Optional[Literal['fenced', 'incomplete_fenced', 'heuristic', 'empty']] = None
    extra: Optional[Dict] = None


class GetPromptsRequest(BaseModel):
    dataset: str
    config: TestConfig
    offset: int = 0
    limit: int = 1000000


class GetPromptByIdRequest(BaseModel):
    dataset: str
    config: TestConfig
    id: int | str


class SubmitRequest(BaseModel):
    dataset: str
    id: int | str
    completion: str
    config: TestConfig


class GetMetricsRequest(BaseModel):
    dataset: str
    config: TestConfig
    results: List[EvalResult]


class GetMetricsFunctionRequest(BaseModel):
    dataset: str
    config: TestConfig


class GetMetricsFunctionResult(BaseModel):
    function: Optional[str]


class CodingDataset(ABC):

    @classmethod
    def get_table_name(cls, dataset_id: str) -> str:
        for registry in server_config.dataset.registry:
            if registry.class_name != cls.__name__:
                continue
            if dataset_id in registry.dataset_tables:
                return registry.dataset_tables[dataset_id]
            else:
                return Template(server_config.dataset.default_dataset_table).substitute(dataset_id=dataset_id)
        raise RuntimeError(f'class {cls.__name__} not in config registry!')

    @classmethod
    async def get_ids(cls, request: GetPromptsRequest) -> List[int | str]:
        prompts = await cls.get_prompts(request)
        return [p.id for p in prompts]

    @classmethod
    @abstractmethod
    async def get_num_problems(self, dataset_id: str) -> int:
        ...

    @classmethod
    @abstractmethod
    async def get_prompts(self, request: GetPromptsRequest) -> List[Prompt]:
        ...

    @classmethod
    @abstractmethod
    async def get_prompt_by_id(self, request: GetPromptByIdRequest) -> Prompt:
        ...

    @classmethod
    @abstractmethod
    async def evaluate_single(self, request: SubmitRequest) -> EvalResult:
        ...
