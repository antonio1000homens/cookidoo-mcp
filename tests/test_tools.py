"""Allowlist contract: mutation tools must not be advertised or callable."""
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

import server as upstream
from src import app


@pytest.mark.asyncio
async def test_exposes_only_read_only_tools():
    mcp = app.build_mcp()
    async with Client(mcp) as client:
        names = {item.name for item in await client.list_tools()}
    assert names == {
        "search_recipes",
        "get_recipe_details",
        "list_custom_recipes",
        "get_custom_recipe",
        "generate_recipe_structure",
    }
    assert "upload_custom_recipe" not in names
    assert "update_custom_recipe" not in names
    assert "connect_to_cookidoo" not in names


@pytest.mark.asyncio
async def test_search_delegates_to_pinned_upstream(monkeypatch):
    monkeypatch.setattr(app, "ensure_credentials", lambda: None)
    upstream_search = AsyncMock(return_value="Found mocked test recipe")
    monkeypatch.setattr(upstream, "search_recipes", upstream_search)
    async with Client(app.build_mcp()) as client:
        result = await client.call_tool("search_recipes", {"query": "madras"})
    assert "Found mocked test recipe" in result.content[0].text
    upstream_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_recipe_is_non_mutating():
    async with Client(app.build_mcp()) as client:
        result = await client.call_tool(
            "generate_recipe_structure",
            {
                "name": "Test",
                "ingredients": "100 g flour",
                "steps": "Mix ingredients",
                "servings": 4,
            },
        )
    assert '"name": "Test"' in result.content[0].text
