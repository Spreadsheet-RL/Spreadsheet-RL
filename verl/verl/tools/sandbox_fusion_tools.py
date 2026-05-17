# Copyright 2025 Bytedance Ltd. and/or its affiliates
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

import asyncio
import logging
import os
import re
import threading
from contextlib import ExitStack
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

import ray

from verl.tools.base_tool import BaseTool
from verl.utils.paths import normalize_thread_dir, normalize_workspace_id
from verl.utils.reward_score.sandbox_fusion.utils import _process_single_case
from verl.utils.rollout_trace import rollout_trace_op

from .schemas import OpenAIFunctionToolSchema, ToolResponse

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))

_JEMALLOC_BG_THREAD_WARNING_RE = re.compile(
    r"^\s*<jemalloc>:[^\n]*background thread creation failed[^\n]*\r?\n?",
    flags=re.MULTILINE,
)
_EXECUTION_POOL_PING_TIMEOUT_S = 2.0


def _strip_benign_runtime_noise(text: str) -> str:
    if not text:
        return ""
    return _JEMALLOC_BG_THREAD_WARNING_RE.sub("", text)


def _uses_localhost(url: str) -> bool:
    try:
        host = urlparse(url).hostname
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


class PoolMode(Enum):
    ThreadMode = 1
    ProcessMode = 2


_SHARED_EXECUTION_POOLS: dict[tuple[Any, ...], Any] = {}
_SHARED_EXECUTION_POOL_CREATIONS: dict[tuple[Any, ...], "_PoolCreationState"] = {}
_SHARED_EXECUTION_POOLS_LOCK = threading.Lock()


class _PoolCreationState:
    def __init__(self):
        self.event = threading.Event()
        self.pool = None
        self.error = None


def _execution_pool_key(
    *,
    num_workers: int,
    enable_global_rate_limit: bool,
    rate_limit: int,
    sandbox_fusion_url: str,
    memory_limit_mb: int,
    include_stderr_on_success: bool,
    require_local_node: bool,
) -> tuple[Any, ...]:
    return (
        num_workers,
        enable_global_rate_limit,
        rate_limit,
        sandbox_fusion_url,
        memory_limit_mb,
        include_stderr_on_success,
        require_local_node,
    )


@ray.remote(concurrency_groups={"acquire": 1, "release": 10})
class TokenBucketWorker:
    def __init__(self, rate_limit: int):
        self.rate_limit = rate_limit
        # this only used for observalability
        self.current_count = 0
        self._semaphore = threading.Semaphore(rate_limit)

    @ray.method(concurrency_group="acquire")
    def acquire(self):
        self._semaphore.acquire()
        self.current_count += 1

    @ray.method(concurrency_group="release")
    def release(self):
        self._semaphore.release()
        self.current_count -= 1

    def get_current_count(self):
        return self.current_count


class ExecutionWorker:
    def __init__(
        self,
        enable_global_rate_limit=True,
        rate_limit=10,
        sandbox_fusion_url="",
        memory_limit_mb=1024,
        include_stderr_on_success=False,
    ):
        self.rate_limit_worker = self._init_rate_limit(rate_limit) if enable_global_rate_limit else None
        self.sandbox_fusion_url = sandbox_fusion_url
        self.memory_limit_mb = memory_limit_mb
        self.include_stderr_on_success = include_stderr_on_success

    def _init_rate_limit(self, rate_limit):
        return TokenBucketWorker.options(num_cpus=0).remote(rate_limit)

    def ping(self):
        return True

    def execute(
        self,
        instance_id,
        code,
        timeout=30,
        language="python",
        workspace_id: Optional[str] = None,
        workspace_paths: Optional[list[str]] = None,
    ) -> str:
        try:
            with ExitStack() as stack:
                if self.rate_limit_worker is not None:
                    ray.get(self.rate_limit_worker.acquire.remote())
                    stack.callback(self.rate_limit_worker.release.remote)
                return _execute_code(
                    instance_id=instance_id,
                    code=code,
                    timeout=timeout,
                    language=language,
                    sandbox_fusion_url=self.sandbox_fusion_url,
                    memory_limit_mb=self.memory_limit_mb,
                    include_stderr_on_success=self.include_stderr_on_success,
                    workspace_id=workspace_id,
                    workspace_paths=workspace_paths,
                )
        except Exception:
            logger.warning("Error when executing code in SandboxFusion worker", exc_info=True)
            raise


def init_execution_pool(
    num_workers: int,
    enable_global_rate_limit=True,
    rate_limit=10,
    mode: PoolMode = PoolMode.ThreadMode,
    sandbox_fusion_url="",
    memory_limit_mb=1024,
    include_stderr_on_success=False,
    require_local_node=False,
):
    if mode == PoolMode.ThreadMode:
        actor_options = {
            "max_concurrency": num_workers,
            "max_restarts": -1,
            "max_task_retries": 0,
            "num_cpus": 0,
        }
        try:
            node_id = ray.get_runtime_context().get_node_id()
        except Exception:
            node_id = None
        if node_id:
            actor_options["scheduling_strategy"] = ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                node_id=node_id,
                soft=not require_local_node,
            )
        return (
            ray.remote(ExecutionWorker)
            .options(**actor_options)
            .remote(
                enable_global_rate_limit=enable_global_rate_limit,
                rate_limit=rate_limit,
                sandbox_fusion_url=sandbox_fusion_url,
                memory_limit_mb=memory_limit_mb,
                include_stderr_on_success=include_stderr_on_success,
            )
        )
    else:
        raise NotImplementedError("Process mode is not implemented yet")
        # return ray.util.multiprocessing.Pool(processes=num_workers)


def _execute_code(
    instance_id,
    code,
    timeout=30,
    language="python",
    sandbox_fusion_url="",
    memory_limit_mb=1024,
    include_stderr_on_success=False,
    workspace_id: Optional[str] = None,
    workspace_paths: Optional[list[str]] = None,
) -> str:
    _result_status, metadata = _process_single_case(
        0,
        None,
        None,
        sandbox_fusion_url,
        code,
        timeout,
        memory_limit_mb,
        language,
        workspace_id=workspace_id,
        workspace_paths=workspace_paths,
        max_retries=1,
    )
    stdout = metadata.get("stdout") or ""
    stderr = metadata.get("stderr") or ""
    run_status = metadata.get("run_status")
    exit_code = metadata.get("exit_code")
    status = metadata.get("status", "unknown_error")

    stdout = _strip_benign_runtime_noise(stdout)
    stderr = _strip_benign_runtime_noise(stderr)

    if run_status == "Finished" and (exit_code == 0 or (exit_code is None and status == "success")):
        actual_output = "Code executed successfully:\n" + stdout + (stderr if include_stderr_on_success else "")
        logger.debug("actual_output from sandbox fusion: %s,%s", actual_output, instance_id)
        return actual_output

    if run_status == "Finished" and exit_code not in (0, None):
        status = "runtime_error"

    compile_stderr = _strip_benign_runtime_noise(metadata.get("compile_stderr") or "")
    api_request_error = metadata.get("api_request_error") or ""
    details = (stdout + stderr) or compile_stderr or api_request_error or ""
    exit_code_str = f" (exit_code={exit_code})" if exit_code is not None else ""
    if details:
        return f"Code execution failed with status: {status}{exit_code_str}\n{details}"
    return f"Code execution failed with status: {status}{exit_code_str}"


class SandboxFusionTool(BaseTool):
    """A tool for executing the code using sanbox fusion image.

    - `get_openai_tool_schema`: return the tool schema in OpenAI format.
    - `create`: create a tool instance for a trajectory.
    - `execute`: execute the tool.
    - `calc_reward`: calculate the reward respect to tool state.
    - `release`: release the tool instance.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        """
        _tool_schema = OpenAIFunctionToolSchema.model_validate({
            "type": "function",
            "function": {
                "name": "code_interpreter",
                "description": "A tool for execute code",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "code needs to be execute and grad",
                        },
                    },
                    "required": ["code"],
                },
            }
        })
        """
        super().__init__(config, tool_schema)
        self._instance_dict = {}
        # TODO: better documentation for the config
        self.num_workers = config.get("num_workers", 10)
        self.rate_limit = config.get("rate_limit", 10)
        self.default_timeout = config.get("default_timeout", 30)
        self.default_language = config.get("default_language", "python")
        self.enable_global_rate_limit = config.get("enable_global_rate_limit", True)
        # Returning stderr on success often includes benign warnings (e.g. jemalloc) that confuse the LLM.
        # Default to stdout-only for successful runs; keep stderr for failures.
        self.include_stderr_on_success = bool(config.get("include_stderr_on_success", False))
        self.sandbox_fusion_url = config.get("sandbox_fusion_url", "")
        self.memory_limit_mb = config.get("memory_limit_mb", 1024)
        if self.sandbox_fusion_url == "":
            raise ValueError("sandbox_fusion_url is not set")
        self.require_local_node = _uses_localhost(self.sandbox_fusion_url)
        self._execution_pool_key = _execution_pool_key(
            num_workers=self.num_workers,
            enable_global_rate_limit=self.enable_global_rate_limit,
            rate_limit=self.rate_limit,
            sandbox_fusion_url=self.sandbox_fusion_url,
            memory_limit_mb=self.memory_limit_mb,
            include_stderr_on_success=self.include_stderr_on_success,
            require_local_node=self.require_local_node,
        )
        log_msg = f"Init SandboxFusionTool with config: {config}"
        logger.info(log_msg)

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

    def _create_execution_pool(self):
        return init_execution_pool(
            num_workers=self.num_workers,
            enable_global_rate_limit=self.enable_global_rate_limit,
            rate_limit=self.rate_limit,
            mode=PoolMode.ThreadMode,
            sandbox_fusion_url=self.sandbox_fusion_url,
            memory_limit_mb=self.memory_limit_mb,
            include_stderr_on_success=self.include_stderr_on_success,
            require_local_node=self.require_local_node,
        )

    async def _get_execution_pool(self):
        pool = _SHARED_EXECUTION_POOLS.get(self._execution_pool_key)
        if pool is not None:
            return pool

        with _SHARED_EXECUTION_POOLS_LOCK:
            pool = _SHARED_EXECUTION_POOLS.get(self._execution_pool_key)
            if pool is not None:
                return pool
            creation = _SHARED_EXECUTION_POOL_CREATIONS.get(self._execution_pool_key)
            should_create = creation is None
            if should_create:
                creation = _PoolCreationState()
                _SHARED_EXECUTION_POOL_CREATIONS[self._execution_pool_key] = creation

        if should_create:
            try:
                pool = self._create_execution_pool()
            except Exception as e:
                creation.error = e
                with _SHARED_EXECUTION_POOLS_LOCK:
                    if _SHARED_EXECUTION_POOL_CREATIONS.get(self._execution_pool_key) is creation:
                        del _SHARED_EXECUTION_POOL_CREATIONS[self._execution_pool_key]
                creation.event.set()
                raise

            with _SHARED_EXECUTION_POOLS_LOCK:
                existing_pool = _SHARED_EXECUTION_POOLS.get(self._execution_pool_key)
                if existing_pool is None:
                    _SHARED_EXECUTION_POOLS[self._execution_pool_key] = pool
                else:
                    pool = existing_pool
                if _SHARED_EXECUTION_POOL_CREATIONS.get(self._execution_pool_key) is creation:
                    del _SHARED_EXECUTION_POOL_CREATIONS[self._execution_pool_key]
            creation.pool = pool
            creation.event.set()
            return pool

        await asyncio.to_thread(creation.event.wait)
        if creation.error is not None:
            raise creation.error
        pool = _SHARED_EXECUTION_POOLS.get(self._execution_pool_key) or creation.pool
        if pool is None:
            raise RuntimeError("SandboxFusion execution pool creation finished without a pool")
        return pool

    async def _execution_pool_is_alive(self, pool) -> bool:
        try:
            return bool(await asyncio.wait_for(pool.ping.remote(), timeout=_EXECUTION_POOL_PING_TIMEOUT_S))
        except Exception:
            return False

    async def _replace_execution_pool(self, failed_pool):
        with _SHARED_EXECUTION_POOLS_LOCK:
            current_pool = _SHARED_EXECUTION_POOLS.get(self._execution_pool_key)
            if current_pool is failed_pool:
                del _SHARED_EXECUTION_POOLS[self._execution_pool_key]
            elif current_pool is not None:
                return current_pool
        return await self._get_execution_pool()

    async def _replace_execution_pool_if_dead(self, failed_pool) -> bool:
        if await self._execution_pool_is_alive(failed_pool):
            return False
        await self._replace_execution_pool(failed_pool)
        return True

    async def create(
        self, instance_id: Optional[str] = None, ground_truth: Optional[str] = None, **kwargs
    ) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {
            "response": "",
            "ground_truth": ground_truth,
            "reward": [],
        }
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs) -> tuple[ToolResponse, float, dict]:
        code = parameters.get("code", "")
        timeout = parameters.get("timeout", self.default_timeout)
        language = parameters.get("language", self.default_language)
        if not isinstance(code, str):
            code = str(code)

        workspace_id = kwargs.get("workspace_id")
        data_source = kwargs.get("data_source")
        ground_truth = kwargs.get("ground_truth")
        thread_dir = normalize_thread_dir(ground_truth)
        extra_info = kwargs.get("extra_info")
        primary_ext = extra_info.get("primary_ext") if isinstance(extra_info, dict) else None
        if not isinstance(primary_ext, str) or not primary_ext.startswith("."):
            primary_ext = ".xlsx"

        workspace_id_for_request: Optional[str] = None
        workspace_paths: Optional[list[str]] = None
        if data_source == "sheet_arena":
            workspace_id_for_request = normalize_workspace_id(workspace_id)
            if workspace_id_for_request is None:
                return ToolResponse(text="Error: workspace_id is missing/invalid."), 0.0, {
                    "status": "error",
                    "error": "missing_workspace_id",
                }
            if thread_dir is None:
                return ToolResponse(text="Error: sheet_arena thread_dir is missing/invalid."), 0.0, {
                    "status": "error",
                    "error": "missing_thread_dir",
                }
            workspace_paths = [f"{thread_dir}/output{primary_ext}"]

        execution_pool = await self._get_execution_pool()
        try:
            result_text = await execution_pool.execute.remote(
                instance_id,
                code,
                timeout,
                language,
                workspace_id_for_request,
                workspace_paths,
            )
        except ray.exceptions.RayActorError as e:
            logger.warning("SandboxFusion execution pool died; recreating pool for future calls: %s", e)
            await self._replace_execution_pool(execution_pool)
            return (
                ToolResponse(text="Error executing code: sandbox execution worker died before finishing the task."),
                0.0,
                {"status": "error", "error": "execution_worker_died"},
            )
        except ray.exceptions.RayTaskError as e:
            pool_recreated = await self._replace_execution_pool_if_dead(execution_pool)
            logger.warning(
                "SandboxFusion execution task failed; pool_recreated=%s: %s",
                pool_recreated,
                e,
            )
            return (
                ToolResponse(text=f"Error executing code: sandbox execution failed: {e}"),
                0.0,
                {"status": "error", "error": "execution_failed"},
            )
        except ray.exceptions.RayError as e:
            pool_recreated = await self._replace_execution_pool_if_dead(execution_pool)
            logger.warning(
                "SandboxFusion Ray execution failed; pool_recreated=%s: %s",
                pool_recreated,
                e,
            )
            return (
                ToolResponse(text=f"Error executing code: sandbox execution failed: {e}"),
                0.0,
                {"status": "error", "error": "execution_failed"},
            )
        # sandbox has no score or metrics, use Nones
        return ToolResponse(text=result_text), None, None

    async def calc_reward(self, instance_id: str, **kwargs) -> str:
        return self._instance_dict[instance_id]["reward"]

    async def release(self, instance_id: str, **kwargs) -> None:
        del self._instance_dict[instance_id]
