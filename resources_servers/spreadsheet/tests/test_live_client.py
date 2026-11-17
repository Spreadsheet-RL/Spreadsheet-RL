import json

from resources_servers.spreadsheet.packaging.check_live_service import Client


def test_local_service_canary_uses_existing_key_without_yaml_dependency(tmp_path):
    key = tmp_path / "api-key"
    key.write_text("canary-fixture-" + "x" * 40)
    config = tmp_path / "service.json"
    config.write_text(json.dumps({"port": 8088, "api_key_file": str(key)}))
    client = Client(service_config=config)
    assert client.url == "http://127.0.0.1:8088"
    assert client.key == key.read_text()
