import json
from copy import deepcopy
from types import SimpleNamespace

from resources_servers.spreadsheet.packaging.audit_agent import AuditConfig, AuditedSpreadsheetAgent
from responses_api_agents.spreadsheet_agent.app import SpreadsheetAgent


async def test_audit_preserves_the_authoritative_reward_and_response(monkeypatch, tmp_path):
    payload = {
        "reward": 1.0,
        "response": {"output": [{"type": "message", "generation_token_ids": [7], "content": []}]},
    }
    result = SimpleNamespace(model_dump=lambda **kwargs: deepcopy(payload))

    async def run(self, request, body):
        return result

    monkeypatch.setattr(SpreadsheetAgent, "run", run)
    path = tmp_path / "model_rollouts.jsonl"
    agent = AuditedSpreadsheetAgent.model_construct(config=AuditConfig.model_construct(audit_path=str(path)))
    returned = await agent.run(None, None)
    assert returned is result
    assert payload["response"]["output"][0]["generation_token_ids"] == [7]
    saved = json.loads(path.read_text())
    assert saved["reward"] == 1.0
    assert "generation_token_ids" not in saved["response"]["output"][0]
    assert path.stat().st_mode & 0o777 == 0o600
