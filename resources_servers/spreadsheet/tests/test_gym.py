import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request, Response


pytest.importorskip("nemo_gym", reason="Gym adapter tests run in the existing Gym runtime")

from nemo_gym.openai_utils import NeMoGymResponseCreateParamsNonStreaming
from nemo_gym.server_utils import ServerClient
from responses_api_agents.simple_agent.app import SimpleAgentConfig
from responses_api_agents.spreadsheet_agent.app import SpreadsheetAgent


def reply(value, status=200):
    encoded = json.dumps(value).encode()
    return SimpleNamespace(
        status=status,
        ok=status < 400,
        read=AsyncMock(return_value=encoded),
        content=SimpleNamespace(read=AsyncMock(return_value=encoded)),
        cookies={},
    )


def model_response(output):
    return {
        "id": "r",
        "created_at": 0,
        "model": "test-policy",
        "object": "response",
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "output": output,
    }


def final_message():
    return {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Done", "annotations": []}],
    }


def agent():
    client = MagicMock(spec=ServerClient)
    config = SimpleAgentConfig(
        host="127.0.0.1",
        port=8001,
        name="spreadsheet_agent",
        entrypoint="app.py",
        max_steps=3,
        resources_server={"type": "resources_servers", "name": "resources"},
        model_server={"type": "responses_api_models", "name": "policy"},
    )
    return SpreadsheetAgent(config=config, server_client=client)


def request():
    return Request({"type": "http", "headers": [(b"cookie", b"spreadsheet_test=signed-session-cookie")]})


@pytest.mark.asyncio
async def test_agent_propagates_call_id_cookies_and_training_tokens():
    server = agent()
    tool = {
        "type": "function_call",
        "name": "write_range",
        "call_id": "provider-call-42",
        "arguments": '{"range":"A1","data":42}',
        "prompt_token_ids": [1, 2],
        "generation_token_ids": [3, 4],
        "generation_log_probs": [-0.1, -0.2],
    }
    server.server_client.post = AsyncMock(
        side_effect=[
            reply(model_response([tool])),
            reply({"changed": True}),
            reply(model_response([final_message()])),
        ]
    )
    result = await server._responses_once(request(), Response(), NeMoGymResponseCreateParamsNonStreaming(input=[]))
    calls = server.server_client.post.call_args_list
    assert calls[1].kwargs["headers"] == {"X-Spreadsheet-Operation-Id": "provider-call-42"}
    assert calls[1].kwargs["cookies"]["spreadsheet_test"] == "signed-session-cookie"
    assert result.output[0].model_dump()["generation_token_ids"] == [3, 4]
    assert calls[2].kwargs["json"].input[0].model_dump()["generation_log_probs"] == [-0.1, -0.2]


@pytest.mark.asyncio
async def test_model_cannot_invoke_lifecycle_or_verifier_endpoints():
    server = agent()
    attack = {"type": "function_call", "name": "verify", "call_id": "c", "arguments": "{}"}
    server.server_client.post = AsyncMock(
        side_effect=[
            reply(model_response([attack])),
            reply(model_response([final_message()])),
        ]
    )
    result = await server._responses_once(request(), Response(), NeMoGymResponseCreateParamsNonStreaming(input=[]))
    assert all(c.kwargs["server_name"] == "policy" for c in server.server_client.post.call_args_list)
    assert "Unknown spreadsheet tool" in result.output[1].output


@pytest.mark.asyncio
async def test_retried_responses_share_one_agent_execution():
    server = agent()
    server.server_client.post = AsyncMock(
        side_effect=[reply({"claimed": True}), reply(model_response([final_message()]))]
    )
    body = NeMoGymResponseCreateParamsNonStreaming(input=[])
    a, b = await asyncio.gather(
        server.responses(request(), Response(), body), server.responses(request(), Response(), body)
    )
    assert a.model_dump() == b.model_dump()
    paths = [c.kwargs["url_path"] for c in server.server_client.post.call_args_list]
    assert paths == ["/begin_execution", "/v1/responses"]
