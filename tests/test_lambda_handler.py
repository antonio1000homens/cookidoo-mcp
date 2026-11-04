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
    assert "update_custom_recipe" not in names
