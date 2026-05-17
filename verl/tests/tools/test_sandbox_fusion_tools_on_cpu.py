import asyncio
from unittest.mock import MagicMock, patch

import pytest
import ray
from ray.exceptions import RayActorError, RayError, RayTaskError

from verl.tools import sandbox_fusion_tools
from verl.tools.sandbox_fusion_tools import ExecutionWorker, PoolMode, SandboxFusionTool, init_execution_pool
from verl.tools.schemas import OpenAIFunctionToolSchema
from verl.utils.reward_score.sandbox_fusion import utils as sandbox_utils


@pytest.fixture(autouse=True)
def clear_shared_execution_pools():
    sandbox_fusion_tools._SHARED_EXECUTION_POOLS.clear()
    sandbox_fusion_tools._SHARED_EXECUTION_POOL_CREATIONS.clear()
    yield
    sandbox_fusion_tools._SHARED_EXECUTION_POOLS.clear()
    sandbox_fusion_tools._SHARED_EXECUTION_POOL_CREATIONS.clear()


@pytest.fixture
def ray_runtime():
    already_initialized = ray.is_initialized()
    if not already_initialized:
        try:
            ray.init(num_cpus=2, include_dashboard=False, ignore_reinit_error=True)
        except Exception as e:
            pytest.skip(f"Ray runtime unavailable: {e}")
    try:
        yield
    finally:
        if not already_initialized:
            ray.shutdown()


def _tool_schema() -> OpenAIFunctionToolSchema:
    return OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": "code_interpreter",
                "description": "Execute Python code.",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        }
    )


@ray.remote(num_cpus=0)
class _PoolOwner:
    async def inspect_pool_reuse(self, config, schema_dict):
        import asyncio
        import gc
        import os

        from verl.tools import sandbox_fusion_tools as remote_sandbox_fusion_tools
        from verl.tools.sandbox_fusion_tools import SandboxFusionTool as RemoteSandboxFusionTool
        from verl.tools.schemas import OpenAIFunctionToolSchema as RemoteOpenAIFunctionToolSchema

        remote_sandbox_fusion_tools._SHARED_EXECUTION_POOLS.clear()
        schema = RemoteOpenAIFunctionToolSchema.model_validate(schema_dict)
        tools = [RemoteSandboxFusionTool(config=config, tool_schema=schema) for _ in range(4)]
        pools = await asyncio.gather(*(tool._get_execution_pool() for tool in tools))
        same_pool = all(pool is pools[0] for pool in pools)
        cache_size = len(remote_sandbox_fusion_tools._SHARED_EXECUTION_POOLS)
        del tools
        gc.collect()
        ping_after_tool_gc = await pools[0].ping.remote()
        pool_actor_id = pools[0]._actor_id.hex()
        return {
            "cache_size": cache_size,
            "owner_pid": os.getpid(),
            "ping_after_tool_gc": ping_after_tool_gc,
            "pool_actor_id": pool_actor_id,
            "same_pool": same_pool,
        }


def test_init_execution_pool_uses_owned_unnamed_actor():
    remote_cls = MagicMock()
    remote_cls.options.return_value.remote.return_value = "actor"

    with (
        patch.object(sandbox_fusion_tools.ray, "remote", return_value=remote_cls),
        patch.object(sandbox_fusion_tools.ray, "get_runtime_context", side_effect=RuntimeError("no ray context")),
    ):
        actor = init_execution_pool(num_workers=4, enable_global_rate_limit=False, mode=PoolMode.ThreadMode)

    assert actor == "actor"
    options = remote_cls.options.call_args.kwargs
    assert options["max_concurrency"] == 4
    assert options["max_restarts"] == -1
    assert options["max_task_retries"] == 0
    assert options["num_cpus"] == 0
    assert "name" not in options
    assert "get_if_exists" not in options


def test_init_execution_pool_uses_hard_affinity_for_localhost():
    remote_cls = MagicMock()
    remote_cls.options.return_value.remote.return_value = "actor"
    context = MagicMock()
    context.get_node_id.return_value = "node-1"
    strategy_cls = MagicMock(return_value="strategy")

    with (
        patch.object(sandbox_fusion_tools.ray, "remote", return_value=remote_cls),
        patch.object(sandbox_fusion_tools.ray, "get_runtime_context", return_value=context),
        patch.object(
            sandbox_fusion_tools.ray.util.scheduling_strategies,
            "NodeAffinitySchedulingStrategy",
            strategy_cls,
        ),
    ):
        actor = init_execution_pool(
            num_workers=1,
            enable_global_rate_limit=False,
            mode=PoolMode.ThreadMode,
            require_local_node=True,
        )

    assert actor == "actor"
    strategy_cls.assert_called_once_with(node_id="node-1", soft=False)
    assert remote_cls.options.call_args.kwargs["scheduling_strategy"] == "strategy"


def test_sandbox_tool_does_not_create_actor_during_init():
    with patch.object(sandbox_fusion_tools, "init_execution_pool") as init_pool:
        tool = SandboxFusionTool(
            config={
                "sandbox_fusion_url": "http://127.0.0.1:8080/run_code",
                "num_workers": 1,
                "enable_global_rate_limit": False,
            },
            tool_schema=_tool_schema(),
    )

    init_pool.assert_not_called()
    assert sandbox_fusion_tools._SHARED_EXECUTION_POOLS == {}
    assert tool.require_local_node is True


@pytest.mark.asyncio
async def test_sandbox_tools_share_process_local_pool():
    config = {
        "sandbox_fusion_url": "http://127.0.0.1:8080/run_code",
        "num_workers": 4,
        "enable_global_rate_limit": False,
    }
    tool_a = SandboxFusionTool(config=config, tool_schema=_tool_schema())
    tool_b = SandboxFusionTool(config=config, tool_schema=_tool_schema())

    with patch.object(sandbox_fusion_tools, "init_execution_pool", return_value="shared-pool") as init_pool:
        pool_a = await tool_a._get_execution_pool()
        pool_b = await tool_b._get_execution_pool()

    assert pool_a == "shared-pool"
    assert pool_b == "shared-pool"
    assert init_pool.call_count == 1


def test_sandbox_tools_share_pool_owned_by_ray_actor(ray_runtime):
    owners = [_PoolOwner.remote() for _ in range(2)]
    results = ray.get(
        [
            owner.inspect_pool_reuse.remote(
                {
                    "sandbox_fusion_url": "http://127.0.0.1:8080/run_code",
                    "num_workers": 4,
                    "enable_global_rate_limit": False,
                },
                _tool_schema().model_dump(mode="json"),
            )
            for owner in owners
        ]
    )

    for result in results:
        assert result["cache_size"] == 1
        assert result["ping_after_tool_gc"] is True
        assert result["same_pool"] is True
    assert results[0]["owner_pid"] != results[1]["owner_pid"]
    assert results[0]["pool_actor_id"] != results[1]["pool_actor_id"]


def test_code_tool_does_not_retry_gateway_timeout():
    response = MagicMock()
    response.status_code = 504

    with (
        patch.object(sandbox_utils.requests, "post", return_value=response) as post,
        patch.object(sandbox_utils.time, "sleep") as sleep,
    ):
        result = sandbox_fusion_tools._execute_code(
            instance_id="instance-id",
            code="print(1)",
            timeout=5,
            language="python",
            sandbox_fusion_url="http://127.0.0.1:8080/run_code",
            memory_limit_mb=1024,
        )

    assert post.call_count == 1
    sleep.assert_not_called()
    assert "Code execution failed with status: api_error" in result
    assert "Gateway Timeout (504) on attempt 1/1" in result


def test_check_correctness_keeps_default_gateway_timeout_retries():
    response = MagicMock()
    response.status_code = 504

    with (
        patch.object(sandbox_utils.requests, "post", return_value=response) as post,
        patch.object(sandbox_utils.time, "sleep") as sleep,
    ):
        result_status, metadata = sandbox_utils._process_single_case(
            0,
            None,
            None,
            "http://127.0.0.1:8080/run_code",
            "print(1)",
            5,
            1024,
            "python",
        )

    assert result_status == -1
    assert post.call_count == sandbox_utils.MAX_RETRIES
    assert sleep.call_count == sandbox_utils.MAX_RETRIES - 1
    assert metadata["status"] == "api_error"
    assert "Gateway Timeout (504) on attempt 3/3" in metadata["api_request_error"]


def _failing_pool(exception, *, ping_delay=0, ping_exception=None, ping_result=True):
    class FailingObjectRef:
        def __await__(self):
            async def raise_error():
                raise exception

            return raise_error().__await__()

    class PingObjectRef:
        def __await__(self):
            async def ping():
                if ping_delay:
                    await asyncio.sleep(ping_delay)
                if ping_exception is not None:
                    raise ping_exception
                return ping_result

            return ping().__await__()

    class FailingExecute:
        def remote(self, *args, **kwargs):
            return FailingObjectRef()

    class Ping:
        def remote(self, *args, **kwargs):
            return PingObjectRef()

    class FailingPool:
        execute = FailingExecute()
        ping = Ping()

    return FailingPool()


class NewPool:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected_error", "ping_exception", "expected_pool_replacement"),
    [
        (RayActorError(error_msg="actor died"), "execution_worker_died", None, True),
        (RayTaskError("execute", "traceback", RuntimeError("boom")), "execution_failed", None, False),
        (RayError("ray failed"), "execution_failed", RayActorError(error_msg="actor died"), True),
    ],
)
async def test_sandbox_tool_handles_ray_failure(exception, expected_error, ping_exception, expected_pool_replacement):
    with patch.object(
        sandbox_fusion_tools,
        "init_execution_pool",
        side_effect=[_failing_pool(exception, ping_exception=ping_exception), NewPool()],
    ) as init_pool:
        tool = SandboxFusionTool(
            {
                "sandbox_fusion_url": "http://127.0.0.1:8080/run_code",
                "num_workers": 1,
                "enable_global_rate_limit": False,
            },
            tool_schema=_tool_schema(),
        )
        response, reward, metrics = await tool.execute("instance-id", {"code": "print(1)"})

    assert init_pool.call_count == (2 if expected_pool_replacement else 1)
    if expected_pool_replacement:
        assert sandbox_fusion_tools._SHARED_EXECUTION_POOLS[tool._execution_pool_key].__class__ is NewPool
    assert reward == 0.0
    assert metrics == {"status": "error", "error": expected_error}
    assert "Error executing code:" in response.text


@pytest.mark.asyncio
async def test_sandbox_tool_replaces_pool_after_health_check_timeout():
    with (
        patch.object(sandbox_fusion_tools, "_EXECUTION_POOL_PING_TIMEOUT_S", 0.01),
        patch.object(
            sandbox_fusion_tools,
            "init_execution_pool",
            side_effect=[_failing_pool(RayError("ray failed"), ping_delay=60), NewPool()],
        ) as init_pool,
    ):
        tool = SandboxFusionTool(
            {
                "sandbox_fusion_url": "http://127.0.0.1:8080/run_code",
                "num_workers": 1,
                "enable_global_rate_limit": False,
            },
            tool_schema=_tool_schema(),
        )
        response, reward, metrics = await tool.execute("instance-id", {"code": "print(1)"})

    assert init_pool.call_count == 2
    assert sandbox_fusion_tools._SHARED_EXECUTION_POOLS[tool._execution_pool_key].__class__ is NewPool
    assert reward == 0.0
    assert metrics == {"status": "error", "error": "execution_failed"}
    assert "Error executing code:" in response.text


def test_execution_worker_reraises_execution_errors():
    worker = ExecutionWorker(
        enable_global_rate_limit=False,
        sandbox_fusion_url="http://127.0.0.1:8080/run_code",
        memory_limit_mb=1024,
        include_stderr_on_success=False,
    )

    with (
        patch.object(sandbox_fusion_tools, "_execute_code", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        worker.execute("instance-id", "print(1)")
