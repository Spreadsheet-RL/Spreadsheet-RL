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

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from sandbox.datasets.types import (
    CodingDataset,
    EvalResult,
    GetMetricsFunctionRequest,
    GetMetricsFunctionResult,
    GetMetricsRequest,
    GetPromptByIdRequest,
    GetPromptsRequest,
    Prompt,
    SubmitRequest,
    TestConfig,
)
from sandbox.registry import get_all_dataset_ids, get_coding_class_by_dataset, get_coding_class_by_name

oj_router = APIRouter()


def get_dataset_cls(dataset_id: str, config: Optional[TestConfig] = None) -> CodingDataset:
    internal_cls = get_coding_class_by_dataset(dataset_id)
    if internal_cls is not None:
        return internal_cls
    if config is None or config.dataset_type is None:
        raise HTTPException(status_code=400, detail=f'no eval class found for dataset {dataset_id}')
    config_cls = get_coding_class_by_name(config.dataset_type)
    if config_cls is None:
        raise HTTPException(status_code=400, detail=f'eval class {config.dataset_type} not found')
    return config_cls


@oj_router.get("/list_datasets", description='List all registered datasets', tags=['datasets'])
async def list_datasets() -> List[str]:
    return get_all_dataset_ids()


@oj_router.post("/list_ids", description='List all ids of a dataset', tags=['datasets'])
async def list_ids(request: GetPromptsRequest) -> List[int | str]:
    dataset = get_dataset_cls(request.dataset, request.config)
    ids = await dataset.get_ids(request)
    return ids


@oj_router.post("/get_prompts", description='Get prompts of a dataset', tags=['datasets'])
async def get_prompt(request: GetPromptsRequest) -> List[Prompt]:
    dataset = get_dataset_cls(request.dataset, request.config)
    prompts = await dataset.get_prompts(request)
    return prompts


@oj_router.post("/get_prompt_by_id", description='Get a single prompt given id in a dataset', tags=['datasets'])
async def get_prompt_by_id(request: GetPromptByIdRequest) -> Prompt:
    dataset = get_dataset_cls(request.dataset, request.config)
    prompt = await dataset.get_prompt_by_id(request)
    return prompt


@oj_router.post("/submit", description='Submit a single problem in a dataset', tags=['datasets'])
async def submit(request: SubmitRequest) -> EvalResult:
    dataset = get_dataset_cls(request.dataset, request.config)
    result = await dataset.evaluate_single(request)
    return result


@oj_router.post("/get_metrics",
                description='Get the metrics given all problem results in a dataset (partially supported)',
                tags=['datasets'])
async def get_metrics(request: GetMetricsRequest) -> Dict[str, Any]:
    dataset = get_dataset_cls(request.dataset, request.config)
    if hasattr(dataset, 'get_metrics'):
        result = await dataset.get_metrics(request.results)
        return result
    else:
        return {}


@oj_router.post("/get_metrics_function",
                description='Get the function to generate the metrics given results (partially supported)',
                tags=['datasets'])
async def get_metrics_function(request: GetMetricsFunctionRequest) -> GetMetricsFunctionResult:
    dataset = get_dataset_cls(request.dataset, request.config)
    if hasattr(dataset, 'get_metrics_function'):
        func = dataset.get_metrics_function()
        return GetMetricsFunctionResult(function=func)
    else:
        return GetMetricsFunctionResult(function=None)
