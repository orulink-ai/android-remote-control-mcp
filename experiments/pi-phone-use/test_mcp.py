"""Mock MCP session lifecycle and diagnostics without a device or credentials."""

import json

import httpx
import pytest

from own_mcp import OwnMcp, McpError


def test_close_deletes_initialized_session_once_before_client_closes():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "DELETE":
            assert request.headers["mcp-session-id"] == "test-session"
            return httpx.Response(204)
        data = json.loads(request.content)
        if data["method"] == "initialize":
            return httpx.Response(
                200,
                json={"result": {"serverInfo": {"name": "test"}}},
                headers={"mcp-session-id": "test-session"},
            )
        return httpx.Response(202)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    mcp = OwnMcp(client=client, url="http://localhost/mcp", headers={})
    mcp.close()
    mcp.close()
    assert [r.method for r in requests] == ["POST", "POST", "DELETE"]
    assert client.is_closed


@pytest.mark.parametrize("failure", ["initialize", "initialized"])
def test_initialization_failure_always_closes_and_sanitizes(failure):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        method = json.loads(request.content)["method"]
        if (failure == "initialize" and method == "initialize") or (
            failure == "initialized" and method.endswith("initialized")
        ):
            return httpx.Response(503, text="secret-response")
        return httpx.Response(
            200,
            json={"result": {"serverInfo": {"name": "test"}}},
            headers={"mcp-session-id": "test-session"},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(McpError) as failed:
        OwnMcp(client=client, url="http://localhost/mcp?secret-token", headers={})
    assert client.is_closed
    assert failed.value.exception_type == "HTTPStatusError"
    assert failed.value.stage == failure
    assert "secret" not in str(failed.value)
    assert sum(r.method == "DELETE" for r in requests) == (failure == "initialized")


def test_session_delete_failure_closes_client_and_is_idempotent():
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.method == "DELETE":
            raise httpx.ReadTimeout("secret-internal-details", request=request)
        method = json.loads(request.content)["method"]
        if method == "initialize":
            return httpx.Response(
                200, json={"result": {"serverInfo": {}}}, headers={"mcp-session-id": "s"}
            )
        return httpx.Response(202)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    mcp = OwnMcp(client=client, url="http://localhost/mcp", headers={})
    mcp.close()
    mcp.close()
    assert mcp.close_error == {"exceptionType": "ReadTimeout", "stage": "close_session"}
    assert calls.count("DELETE") == 1
    assert client.is_closed


def test_definite_stale_reference_remains_recoverable_without_echoing_server_details():
    def respond(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        method = json.loads(request.content)["method"]
        if method == "initialize":
            return httpx.Response(200, json={"result": {"serverInfo": {}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "node not found in accessibility tree; secret extra details",
                        }
                    ],
                }
            },
        )

    mcp = OwnMcp(
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        url="http://localhost/mcp",
        headers={},
    )
    try:
        with pytest.raises(McpError, match="not found in accessibility tree") as failed:
            mcp.call("android_click_node", {"node_id": "old"})
        assert "secret" not in str(failed.value)
    finally:
        mcp.close()
