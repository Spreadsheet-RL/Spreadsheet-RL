# Copyright 2026 Bytedance Ltd. and/or its affiliates
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
from collections.abc import Awaitable, Callable
from typing import Any


def tool_calls_are_parallel_safe(tool_calls: list[Any], *, active_tools: dict[str, Any]) -> bool:
    return all(
        tool_call.name not in active_tools or getattr(active_tools[tool_call.name], "parallel_safe", False)
        for tool_call in tool_calls
    )


def limit_tool_calls(
    tool_calls: list[Any],
    *,
    active_tools: dict[str, Any],
    max_parallel_calls: int,
    max_serial_calls: int | None,
) -> list[Any]:
    return partition_tool_calls(
        tool_calls,
        active_tools=active_tools,
        max_parallel_calls=max_parallel_calls,
        max_serial_calls=max_serial_calls,
    )[0]


def partition_tool_calls(
    tool_calls: list[Any],
    *,
    active_tools: dict[str, Any],
    max_parallel_calls: int,
    max_serial_calls: int | None,
) -> tuple[list[Any], list[Any], list[Any]]:
    parallel_limit = max(0, max_parallel_calls)
    limited = list(tool_calls[:parallel_limit])
    parallel_overflow = list(tool_calls[parallel_limit:])
    if max_serial_calls is None or tool_calls_are_parallel_safe(limited, active_tools=active_tools):
        return limited, [], parallel_overflow
    execute_count = max(0, max_serial_calls)
    return limited[:execute_count], limited[execute_count:], parallel_overflow


async def execute_tool_calls(
    tool_calls: list[Any],
    *,
    active_tools: dict[str, Any],
    call_tool: Callable[[Any], Awaitable[Any]],
) -> list[Any]:
    if tool_calls_are_parallel_safe(tool_calls, active_tools=active_tools):
        return await asyncio.gather(*(call_tool(tool_call) for tool_call in tool_calls))

    responses = []
    for tool_call in tool_calls:
        responses.append(await call_tool(tool_call))
    return responses
