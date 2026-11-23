"""Offline regression checks for Cookidoo image-status reporting.

No test uses a live Cookidoo account or retrieves an external photograph.
"""
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image

from src import app, recipe_image_status as status, recipe_images as images

ID = "01M84RRM1HG27PGCDWXK765YSG"
IMAGE_URL = "https://cdn.example.org/photos/recipe.png"


def fake_service(reference="", *, listed=True, readable=True):
    recipe = SimpleNamespace(id=ID, name="Example custom recipe", image=reference)
    listing = [SimpleNamespace(id=ID)] if listed else []
    return SimpleNamespace(
        list_custom_recipes=AsyncMock(return_value=listing),
        get_custom_recipe=AsyncMock(
            return_value=(recipe, []) if readable else None,
            side_effect=None if readable else LookupError("not yet visible")
        ),
        update_custom_recipe=AsyncMock(),
    )


def prepared_image():
    output = io.BytesIO()
    Image.new("RGB", (120, 120), (130, 20, 40)).save(output, format="PNG")
    return images._normalize_image(output.getvalue())


async def read(service, **kwargs):
    return json.loads(await status.get_status(
        ID, ensure_credentials=lambda: None, get_service=lambda: service,
        **kwargs,
    ))


@pytest.mark.asyncio
async def test_no_image_reference_is_missing_not_verified():
    service = fake_service()
    downloader = AsyncMock(side_effect=AssertionError("download must not run"))
    result = await read(service, download_image=downloader)
    assert result["image_reference"] is None
    assert result["image_reference_present"] is False
    assert result["image_status"] == "missing"
    assert result["image_retrievable"] is False
    assert result["image_present_and_verified"] is False
    assert result["ownership_verified_by_list"] is True
    downloader.assert_not_awaited()
    service.update_custom_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_https_image_is_retrieved_and_verified():
    service = fake_service(IMAGE_URL)
    picture = prepared_image()
    downloader = AsyncMock(return_value=picture)
    result = await read(service, download_image=downloader)
    assert result["image_reference"] == IMAGE_URL
    assert result["image_status"] == "present_and_verified"
    assert result["retrieval_attempted"] is True
    assert result["image_retrievable"] is True
    assert result["image_present_and_verified"] is True
    assert result["image_sha256"] == picture.sha256
    downloader.assert_awaited_once_with(IMAGE_URL)


@pytest.mark.asyncio
async def test_broken_url_is_not_reported_as_verified():
    service = fake_service(IMAGE_URL)
    downloader = AsyncMock(side_effect=ToolError("image URL returned 404"))
    result = await read(service, download_image=downloader)
    assert result["image_reference_present"] is True
    assert result["image_status"] == "unreachable"
    assert result["retrieval_attempted"] is True
    assert result["image_retrievable"] is False
    assert result["image_present_and_verified"] is False
    assert "404" not in json.dumps(result)
    downloader.assert_awaited_once()


@pytest.mark.asyncio
async def test_relative_media_reference_is_present_but_unverifiable():
    service = fake_service("prod/img/customer-recipe/photo.jpg")
    downloader = AsyncMock(side_effect=AssertionError("must not fetch relative path"))
    result = await read(service, download_image=downloader)
    assert result["image_reference_present"] is True
    assert result["image_status"] == "reference_not_fetchable"
    assert result["retrieval_attempted"] is False
    assert result["image_retrievable"] is None
    assert result["image_present_and_verified"] is False
    downloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_created_recipe_missing_from_list_is_not_assumed_owned():
    service = fake_service(IMAGE_URL, listed=False)
    result = await read(service, download_image=AsyncMock(return_value=prepared_image()))
    assert result["recipe_accessible_by_id"] is True
    assert result["recipe_list_status"] == "absent"
    assert result["ownership_verified_by_list"] is False
    assert result["upload_eligibility"] == "not_verified_in_custom_recipe_list"
    assert result["image_retrievable"] is True
    service.update_custom_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipe_missing_from_list_and_not_readable_is_not_marked_imageless():
    service = fake_service(listed=False, readable=False)
    result = await read(service)
    assert result["recipe_accessible_by_id"] is False
    assert result["recipe_list_status"] == "absent"
    assert result["image_status"] == "not_checked"
    assert result["image_reference_present"] is None
    assert result["image_retrievable"] is None


@pytest.mark.asyncio
async def test_failed_recipe_listing_does_not_mean_missing(monkeypatch):
    service = fake_service()
    service.list_custom_recipes.side_effect = TimeoutError("private upstream")
    result = await read(service)
    assert result["recipe_list_status"] == "unavailable"
    assert result["ownership_verified_by_list"] is False
    assert result["recipe_accessible_by_id"] is True
    assert result["upload_eligibility"] == "not_verified_in_custom_recipe_list"


@pytest.mark.asyncio
async def test_invalid_id_is_rejected_without_network():
    with pytest.raises(ToolError, match="Invalid"):
        await status.get_status(
            "../bad", ensure_credentials=lambda: 1 / 0,
            get_service=lambda: 1 / 0,
        )


@pytest.mark.asyncio
async def test_status_tool_is_always_read_only_even_when_uploads_disabled(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "false")
    async with Client(app.build_mcp()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert "get_custom_recipe_image_status" in tools
    assert tools["get_custom_recipe_image_status"].annotations.readOnlyHint is True
    assert "set_custom_recipe_image" not in tools
