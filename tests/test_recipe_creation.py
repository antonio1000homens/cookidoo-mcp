"""Server-enforced recipe validation, preview and safe, idempotent creation."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import app, recipe_creation

RECIPE = {
    "name": "Chicken Madras (TM7) - Automated test",
    "ingredients": ["500 g chicken thighs", "200 g tomatoes"],
    "steps": [
        {"text": "Blend tomatoes {settings}.", "settings": [{"time": 20, "speed": "6"}]},
        {"text": "Cook chicken {settings}.", "settings": [
            {"time": 1080, "temperature": "100", "speed": "soft", "reverse": True},
        ]},
    ],
    "servings": 4,
    "prep_time": 15,
    "total_time": 55,
}
JSON = json.dumps(RECIPE)


def test_preview_has_exact_content_confirmation_and_guided_steps():
    data = json.loads(recipe_creation.preview(JSON))
    assert data["action"] == "PREVIEW_ONLY"
    assert data["guided_step_count"] == 2
    assert data["recipe"]["steps"][1]["settings"][0]["reverse"] is True
    assert data["confirmation"].startswith(recipe_creation.CONFIRM_PREFIX)
    assert data["recipe"]["name"] == RECIPE["name"]


def test_confirmation_is_order_independent_but_change_sensitive():
    _, a, _ = recipe_creation.validate_recipe(JSON)
    _, b, _ = recipe_creation.validate_recipe(json.dumps(dict(reversed(list(RECIPE.items())))))
    assert a == b
    updated = dict(RECIPE, name="Chicken Madras (TM7) UPDATED")
    _, c, _ = recipe_creation.validate_recipe(json.dumps(updated))
    assert a != c


@pytest.mark.parametrize("payload", [
    "{broken json",
    json.dumps(dict(RECIPE, servings=30)),
    json.dumps(dict(RECIPE, steps=[])),
    json.dumps(dict(RECIPE, steps=["Step " + "x" * 1001])),
    json.dumps(dict(RECIPE, ingredients=["test"] * 101)),
    json.dumps(dict(RECIPE, steps=[{"text": "Cook {settings}", "settings": [
        {"time": 10, "speed": "99"}]}])),
    json.dumps(dict(RECIPE, steps=[{"text": "Cook without placeholder", "settings": [
        {"time": 10, "speed": "1"}]}])),
    " " * (recipe_creation.MAX_RECIPE_BYTES + 1),
])
def test_bad_recipe_is_rejected_without_access_to_cookidoo(payload):
    with pytest.raises(ToolError):
        recipe_creation.validate_recipe(payload)


@pytest.mark.asyncio
async def test_readonly_mode_does_not_advertise_upload(monkeypatch):
    monkeypatch.delenv("COOKIDOO_RECIPE_CREATION_ENABLED", raising=False)
    async with Client(app.build_mcp()) as client:
        names = {x.name for x in await client.list_tools()}
        assert "preview_custom_recipe_upload" in names
        assert "upload_custom_recipe" not in names
        result = await client.call_tool("preview_custom_recipe_upload", {"recipe_json": JSON})
    assert "PREVIEW_ONLY" in result.content[0].text


@pytest.mark.asyncio
async def test_upload_is_listed_only_when_enabled(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    async with Client(app.build_mcp()) as client:
        names = {x.name for x in await client.list_tools()}
    assert "upload_custom_recipe" in names
    assert "update_custom_recipe" not in names
    assert "connect_to_cookidoo" not in names


@pytest.mark.asyncio
async def test_disabled_mode_fails_before_credentials(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "false")
    with pytest.raises(ToolError, match="disabled"):
        await recipe_creation.create(JSON, "", ensure_credentials=lambda: 1 / 0,
                                     get_service=lambda: 1 / 0)


@pytest.mark.asyncio
async def test_bad_confirmation_fails_before_credentials(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    with pytest.raises(ToolError, match="confirmation"):
        await recipe_creation.create(JSON, "wrong", ensure_credentials=lambda: 1 / 0,
                                     get_service=lambda: 1 / 0)


@pytest.mark.asyncio
async def test_duplicate_in_account_does_not_reserve(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_WRITE_GUARD_BUCKET", "aws2022-lambda-code")
    service = SimpleNamespace(list_custom_recipes=AsyncMock(return_value=[
        SimpleNamespace(name=RECIPE["name"].upper())]), create_custom_recipe=AsyncMock())
    monkeypatch.setattr(recipe_creation, "_reserve_title", lambda *args: 1 / 0)
    with pytest.raises(ToolError, match="already exists"):
        await recipe_creation.create(
            JSON, json.loads(recipe_creation.preview(JSON))["confirmation"],
            ensure_credentials=lambda: None, get_service=lambda: service,
        )
    service.create_custom_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_reserves_title_once_and_reports_created_id(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_WRITE_GUARD_BUCKET", "aws2022-lambda-code")
    reservations = []
    monkeypatch.setattr(recipe_creation, "_reserve_title", lambda *args: reservations.append(args))
    service = SimpleNamespace(
        list_custom_recipes=AsyncMock(return_value=[]),
        create_custom_recipe=AsyncMock(return_value=SimpleNamespace(
            id="recipe-test-id", name=RECIPE["name"], url="https://cookidoo.co.uk/created-recipes/test")),
    )
    result = json.loads(await recipe_creation.create(
        JSON, json.loads(recipe_creation.preview(JSON))["confirmation"],
        ensure_credentials=lambda: None, get_service=lambda: service,
    ))
    assert result["created"] is True
    assert result["recipe_id"] == "recipe-test-id"
    assert len(reservations) == 1
    assert service.create_custom_recipe.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_upload_failure_does_not_auto_retry(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_WRITE_GUARD_BUCKET", "aws2022-lambda-code")
    called = []
    monkeypatch.setattr(recipe_creation, "_reserve_title", lambda *args: called.append(args))
    service = SimpleNamespace(list_custom_recipes=AsyncMock(return_value=[]),
                              create_custom_recipe=AsyncMock(side_effect=TimeoutError("sensitive response")))
    with pytest.raises(ToolError, match="Do not retry"):
        await recipe_creation.create(
            JSON, json.loads(recipe_creation.preview(JSON))["confirmation"],
            ensure_credentials=lambda: None, get_service=lambda: service)
    assert len(called) == 1
    service.create_custom_recipe.assert_awaited_once()


def test_s3_precondition_failure_prevents_duplicate(monkeypatch):
    import boto3
    body = {"Error": {"Code": "PreconditionFailed", "Message": "Secret context"}}
    class StubS3:
        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            assert kwargs["ServerSideEncryption"] == "AES256"
            assert kwargs["Key"].startswith(recipe_creation.GUARD_PREFIX)
            assert kwargs["Body"] == b"a" * 64
            raise ClientError(body, "PutObject")
    monkeypatch.setattr(boto3, "client", lambda service: StubS3())
    recipe, _, _ = recipe_creation.validate_recipe(JSON)
    with pytest.raises(ToolError, match="already been submitted"):
        recipe_creation._reserve_title("aws2022-lambda-code", recipe, "a" * 64)
