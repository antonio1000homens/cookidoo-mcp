"""Lambda Function URL integration contract, without any Cookidoo account access."""
import importlib
import json

from src import auth


def _event(path="/mcp", method="POST", body=None, headers=None):
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json", **(headers or {})},
        "requestContext": {
            "accountId": "anonymous",
            "apiId": "test",
            "domainName": "example.lambda-url.eu-west-2.on.aws",
            "domainPrefix": "example",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "offline-test",
            },
            "requestId": "test",
            "routeKey": "$default",
            "stage": "$default",
            "time": "01/Jan/2026:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "isBase64Encoded": False,
        "body": json.dumps(body) if body is not None else None,
    }


def test_lambda_denies_direct_origin_without_signed_identity(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "https://test.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "dummy-audience")
    import src.lambda_handler as runtime

    importlib.reload(runtime)
    assert runtime.handler(_event(), {})["statusCode"] == 403
    assert runtime.handler(
        _event(headers={"cf-access-jwt-assertion": "tampered"}), {}
    )["statusCode"] == 403
    assert runtime.handler(_event(path="/health", method="GET"), {})["statusCode"] == 403


def test_lambda_json_mcp_tools_list_is_read_only(monkeypatch):
    # Authentication is stubbed *only for the offline transport test*.
    # Actual production verifier cryptographically validates the Access JWT.
    class StubVerifier:
        def verify(self, token):
            if token != "offline-only-token":
                raise ValueError("Access denied")

    monkeypatch.setattr(
        auth.AccessVerifier,
        "from_environment",
        classmethod(lambda cls: StubVerifier()),
    )
    monkeypatch.setattr(
        auth.AccessGate,
        "_expected_origin_token",
        lambda _self: "offline-origin-token",
    )
    import src.lambda_handler as runtime

    importlib.reload(runtime)
    result = runtime.handler(
        _event(
            headers={
                "cf-access-jwt-assertion": "offline-only-token",
                "authorization": "Bearer offline-origin-token",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-06-18",
            },
            body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        ),
        {},
    )
    assert result["statusCode"] == 200, result
    response = json.loads(result["body"])
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "search_recipes" in names
    assert "get_recipe_details" in names
    assert "upload_custom_recipe" not in names
    assert "preview_custom_recipe_update" in names
    assert "preview_custom_recipe_image" in names
    assert "preview_copy_custom_recipe_image" in names
    assert "set_custom_recipe_image" not in names
    assert "update_custom_recipe" not in names



def test_lambda_mcp_initialize_and_tool_discovery_with_writes_enabled(monkeypatch):
    """Real Lambda/Mangum transport, including initialize, notification and tools/list.

    Access credentials are synthetic only; no upstream Cookidoo login or AWS
    SecureString is read during this offline test.
    """
    class StubVerifier:
        def verify(self, token):
            if token != "synthetic-test-access-jwt":
                raise ValueError("Unauthorised")

    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setattr(
        auth.AccessVerifier,
        "from_environment",
        classmethod(lambda cls: StubVerifier()),
    )
    monkeypatch.setattr(
        auth.AccessGate,
        "_expected_origin_token",
        lambda _self: "synthetic-test-origin-bearer",
    )

    import src.lambda_handler as runtime
    importlib.reload(runtime)
    headers = {
        "cf-access-jwt-assertion": "synthetic-test-access-jwt",
        "authorization": "Bearer synthetic-test-origin-bearer",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    initialize = runtime.handler(
        _event(
            headers=headers,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "offline-transport-test", "version": "1.0"},
                },
            },
        ),
        {},
    )
    assert initialize["statusCode"] == 200, initialize
    init_json = json.loads(initialize["body"])
    assert init_json["id"] == 1
    assert "serverInfo" in init_json["result"], initialize
    assert "protocolVersion" in init_json["result"], initialize

    notification = runtime.handler(
        _event(
            headers=headers,
            body={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        ),
        {},
    )
    assert notification["statusCode"] in (200, 202, 204), notification

    tools = runtime.handler(
        _event(
            headers=headers,
            body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ),
        {},
    )
    assert tools["statusCode"] == 200, tools
    payload = json.loads(tools["body"])
    names = {item["name"] for item in payload["result"]["tools"]}
    assert len(names) == 12, names
    assert "set_custom_recipe_image" in names
    assert "copy_custom_recipe_image" in names
    assert "upload_custom_recipe" in names
    assert "preview_custom_recipe_upload" in names
    assert "update_custom_recipe" not in names
    assert all(
        isinstance(item.get("inputSchema"), dict)
        and item["inputSchema"].get("type") == "object"
        for item in payload["result"]["tools"]
    )


def test_authorized_transport_diagnostics_do_not_log_credentials(monkeypatch, caplog):
    class StubVerifier:
        def verify(self, token):
            return None

    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setattr(
        auth.AccessVerifier,
        "from_environment",
        classmethod(lambda cls: StubVerifier()),
    )
    monkeypatch.setattr(
        auth.AccessGate,
        "_expected_origin_token",
        lambda _self: "synthetic-origin-secret-never-log",
    )
    import src.lambda_handler as runtime
    importlib.reload(runtime)
    with caplog.at_level("WARNING", logger="src.auth"):
        result = runtime.handler(
            _event(
                headers={
                    "cf-access-jwt-assertion": "synthetic-access-token-never-log",
                    "authorization": "Bearer synthetic-origin-secret-never-log",
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-06-18",
                },
                body={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
            ),
            {},
        )
    assert result["statusCode"] == 200, result
    assert "Cookidoo MCP transport: method=POST path=/mcp" in caplog.text
    assert "status=200" in caplog.text
    assert "mime=application/json" in caplog.text
    assert "synthetic-access-token-never-log" not in caplog.text
    assert "synthetic-origin-secret-never-log" not in caplog.text
    assert "tools/list" not in caplog.text



def test_modern_2026_mcp_discovery_and_tools_list_require_routing_headers(monkeypatch):
    """Exercise real pinned FastMCP 4 / MCP SDK 2 modern HTTP wire format."""
    class StubVerifier:
        def verify(self, token):
            if token != "synthetic-valid-access":
                raise ValueError("Invalid")

    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setattr(
        auth.AccessVerifier, "from_environment",
        classmethod(lambda cls: StubVerifier()),
    )
    monkeypatch.setattr(
        auth.AccessGate, "_expected_origin_token",
        lambda self: "synthetic-valid-origin",
    )
    import src.lambda_handler as runtime
    importlib.reload(runtime)

    headers = {
        "cf-access-jwt-assertion": "synthetic-valid-access",
        "authorization": "Bearer synthetic-valid-origin",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2026-07-28",
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": "lambda-modern-protocol-test", "version": "1.0.0"
        },
    }

    discover = runtime.handler(
        _event(
            headers={**headers, "mcp-method": "server/discover"},
            body={
                "jsonrpc": "2.0", "id": 11, "method": "server/discover",
                "params": {"_meta": meta},
            },
        ),
        {},
    )
    assert discover["statusCode"] == 200, discover
    data = json.loads(discover["body"])
    assert data["id"] == 11
    assert "result" in data, discover

    listing = runtime.handler(
        _event(
            headers={**headers, "mcp-method": "tools/list"},
            body={
                "jsonrpc": "2.0", "id": 12, "method": "tools/list",
                "params": {"_meta": meta},
            },
        ),
        {},
    )
    assert listing["statusCode"] == 200, listing
    tools = json.loads(listing["body"])["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert "upload_custom_recipe" in names
    assert "preview_custom_recipe_upload" in names
    assert len(names) == 12

    missing = runtime.handler(
        _event(
            headers=headers,
            body={
                "jsonrpc": "2.0", "id": 13, "method": "server/discover",
                "params": {"_meta": meta},
            },
        ),
        {},
    )
    assert missing["statusCode"] == 400, missing
    error = json.loads(missing["body"])
    assert error.get("error", {}).get("code") == -32020, missing
