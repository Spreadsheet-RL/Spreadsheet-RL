"""Spreadsheet orchestration with stable per-function-call operation IDs.

The Responses loop follows simple_agent so training token IDs/logprobs and usage
remain attached to their original model outputs. Keep it aligned with that agent.
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import List
from uuid import uuid4

from fastapi import HTTPException, Request, Response
from pydantic import Field, ValidationError

from nemo_gym.base_responses_api_agent import Body
from nemo_gym.openai_utils import (
    NeMoGymEasyInputMessage,
    NeMoGymFunctionCallOutput,
    NeMoGymResponse,
    NeMoGymResponseCreateParamsNonStreaming,
    NeMoGymResponseFunctionToolCall,
    NeMoGymResponseOutputMessage,
)
from nemo_gym.server_utils import get_response_json, raise_for_status
from resources_servers.spreadsheet.schemas import TOOL_MODELS
from responses_api_agents.simple_agent.app import (
    SimpleAgent,
    SimpleAgentConfig,
    SimpleAgentRunRequest,
    SimpleAgentVerifyRequest,
    SimpleAgentVerifyResponse,
)


class SpreadsheetAgent(SimpleAgent):
    config: SimpleAgentConfig
    response_tasks: dict = Field(default_factory=dict, exclude=True)

    async def responses(
        self, request: Request, response: Response, body: NeMoGymResponseCreateParamsNonStreaming = Body()
    ) -> NeMoGymResponse:
        cookie_values = {k: v for k, v in request.cookies.items() if k.startswith("spreadsheet_")}
        if not cookie_values:
            raise HTTPException(409, "Spreadsheet session cookie is missing")
        key = hashlib.sha256(json.dumps([cookie_values, body.model_dump()], sort_keys=True).encode()).hexdigest()
        now = time.monotonic()
        for old_key, (created, task) in list(self.response_tasks.items()):
            if task.done() and now - created > 300:
                self.response_tasks.pop(old_key)
        if key not in self.response_tasks:
            if len(self.response_tasks) >= 128:
                raise HTTPException(503, "Spreadsheet agent response cache is full")
            execution_id = uuid4().hex

            async def perform():
                claimed = await self.server_client.post(
                    server_name=self.config.resources_server.name,
                    url_path="/begin_execution",
                    json={"execution_id": execution_id},
                    cookies=request.cookies,
                )
                await raise_for_status(claimed)
                output_response = Response()
                result = await self._responses_once(request, output_response, body)
                return result, output_response.raw_headers

            self.response_tasks[key] = (now, asyncio.create_task(perform()))
        result, headers = await asyncio.shield(self.response_tasks[key][1])
        for name, value in headers:
            if name.lower() == b"set-cookie":
                response.raw_headers.append((name, value))
        return result

    async def _responses_once(
        self,
        request: Request,
        response: Response,
        body: NeMoGymResponseCreateParamsNonStreaming = Body(),
    ) -> NeMoGymResponse:
        body = body.model_copy(deep=True)

        if isinstance(body.input, str):
            body.input = [NeMoGymEasyInputMessage(role="user", content=body.input)]

        new_outputs = []
        usage = None
        step = 0
        model_server_cookies = None  # update the cookies on every model response
        resources_server_cookies = request.cookies  # update the cookies on every resources server response

        while True:
            step += 1
            new_body = body.model_copy(update={"input": body.input + new_outputs})

            model_response = await self.server_client.post(
                server_name=self.config.model_server.name,
                url_path="/v1/responses",
                json=new_body,
                cookies=model_server_cookies,
            )
            # We raise for status here since we expect model calls to always work.
            await raise_for_status(model_response)
            model_response_json = await get_response_json(model_response)
            model_server_cookies = dict(model_server_cookies or {}) | dict(model_response.cookies)
            try:
                model_response = NeMoGymResponse.model_validate(model_response_json)
            except ValidationError as e:
                raise RuntimeError(
                    f"Received an invalid response from model server: {json.dumps(model_response_json)}"
                ) from e

            output = model_response.output
            new_outputs.extend(output)

            if not usage:
                usage = model_response.usage
                model_response.usage = None

            if usage and model_response.usage:
                usage.input_tokens += model_response.usage.input_tokens
                usage.output_tokens += model_response.usage.output_tokens
                usage.total_tokens += model_response.usage.total_tokens

                # TODO support more advanced token details
                usage.input_tokens_details.cached_tokens = 0
                usage.output_tokens_details.reasoning_tokens = 0

            if model_response.incomplete_details:
                break

            all_fn_calls: List[NeMoGymResponseFunctionToolCall] = [o for o in output if o.type == "function_call"]
            all_output_messages: List[NeMoGymResponseOutputMessage] = [
                o for o in output if o.type == "message" and o.role == "assistant"
            ]
            if not all_fn_calls and all_output_messages:
                break

            for output_function_call in all_fn_calls:
                if output_function_call.name not in TOOL_MODELS:
                    new_outputs.append(
                        NeMoGymFunctionCallOutput(
                            type="function_call_output",
                            call_id=output_function_call.call_id,
                            output=json.dumps({"error": "Unknown spreadsheet tool"}),
                        )
                    )
                    continue
                try:
                    parsed_arguments = json.loads(output_function_call.arguments)
                except (json.JSONDecodeError, TypeError) as e:
                    # Model produced malformed tool-call arguments. Surface the
                    # error back as a tool response so the rollout can continue
                    # (or terminate with a low reward) instead of crashing the
                    # whole batch on json.loads.
                    tool_response = NeMoGymFunctionCallOutput(
                        type="function_call_output",
                        call_id=output_function_call.call_id,
                        # Use repr(e) so the exception type name is always
                        # included even when str(e) would be empty.
                        output=json.dumps({"error": f"Invalid tool call arguments: {e!r}"}),
                    )
                    new_outputs.append(tool_response)
                    continue

                api_response = await self.server_client.post(
                    server_name=self.config.resources_server.name,
                    url_path=f"/{output_function_call.name}",
                    json=parsed_arguments,
                    cookies=resources_server_cookies,
                    headers={"X-Spreadsheet-Operation-Id": output_function_call.call_id},
                )
                # We don't raise for status here since it's a valid return for the API to error e.g. if the model outputs an invalid call or something.
                if api_response.status >= 500:
                    await raise_for_status(api_response)
                resources_server_cookies = dict(resources_server_cookies or {}) | dict(api_response.cookies)

                tool_response = NeMoGymFunctionCallOutput(
                    type="function_call_output",
                    call_id=output_function_call.call_id,
                    output=(await api_response.content.read()).decode(),
                )
                new_outputs.append(tool_response)

            # Check if max steps is not None and if we have exhausted it.
            if self.config.max_steps and step >= self.config.max_steps:
                break

        # Propogate any extra cookies necessary for downstream verification
        for k, v in (*resources_server_cookies.items(), *model_server_cookies.items()):
            response.set_cookie(k, v)

        model_response.output = new_outputs
        model_response.usage = usage
        return model_response

    async def run(self, request: Request, body: SimpleAgentRunRequest) -> SimpleAgentVerifyResponse:
        # Never place rollout IDs in static dataset rows: every GRPO sample must
        # receive a fresh identity. A caller may supply one to retry a /run.
        values = body.model_dump()
        values.setdefault("rollout_id", uuid4().hex)
        cookies = request.cookies
        seeded = False
        try:
            seed = await self.server_client.post(
                server_name=self.config.resources_server.name,
                url_path="/seed_session",
                json=values,
                cookies=cookies,
            )
            await raise_for_status(seed)
            cookies, seeded = seed.cookies, True
            response = await self.server_client.post(
                server_name=self.config.name,
                url_path="/v1/responses",
                json=body.responses_create_params,
                cookies=cookies,
            )
            await raise_for_status(response)
            cookies = response.cookies
            verify = SimpleAgentVerifyRequest.model_validate(values | {"response": await get_response_json(response)})
            result = await self.server_client.post(
                server_name=self.config.resources_server.name,
                url_path="/verify",
                json=verify.model_dump(),
                cookies=cookies,
            )
            await raise_for_status(result)
            return SimpleAgentVerifyResponse.model_validate(await get_response_json(result))
        finally:
            if seeded:
                try:
                    async with asyncio.timeout(5):
                        closed = await self.server_client.post(
                            server_name=self.config.resources_server.name,
                            url_path="/close_session",
                            json={},
                            cookies=cookies,
                        )
                        await raise_for_status(closed)
                except Exception:
                    logging.getLogger(__name__).warning("Spreadsheet session retained for TTL cleanup")


if __name__ == "__main__":
    SpreadsheetAgent.run_webserver()
