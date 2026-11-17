"""Smoke-only agent entrypoint that preserves results before trainer slimming."""

import json
import os

from fastapi import Request

from responses_api_agents.simple_agent.app import SimpleAgentRunRequest, SimpleAgentVerifyResponse
from responses_api_agents.spreadsheet_agent.app import SpreadsheetAgent, SpreadsheetAgentConfig


class AuditConfig(SpreadsheetAgentConfig):
    audit_path: str


class AuditedSpreadsheetAgent(SpreadsheetAgent):
    config: AuditConfig

    async def run(self, request: Request, body: SimpleAgentRunRequest) -> SimpleAgentVerifyResponse:
        result = await super().run(request, body)
        record = result.model_dump(mode="json")
        for item in record["response"]["output"]:
            for key in ("prompt_token_ids", "generation_token_ids", "generation_log_probs"):
                item.pop(key, None)
        # A single worker writes synchronously between awaits; append cannot
        # interleave with another request in this event loop. Keep mode private.
        fd = os.open(self.config.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result


if __name__ == "__main__":
    AuditedSpreadsheetAgent.run_webserver()
