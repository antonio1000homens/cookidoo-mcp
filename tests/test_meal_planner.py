"""Offline contract tests for My Week read and controlled scheduling."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cookidoo_api.types import CookidooCalendarDay, CookidooCalendarDayRecipe
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import app, meal_planner


DATE = "2099-11-21"
CUSTOM = "01M6V1VNJHT22PZ1DXPXASDFR0"


def day_with_custom():
    return CookidooCalendarDay(
        id=DATE, title=DATE,
        recipes=[
            CookidooCalendarDayRecipe(
                id=CUSTOM, name="Chicken Madras TM7 – Browning Edition",
                total_time=4800, thumbnail=None, image=None,
                url=f"https://cookidoo.co.uk/created-recipes/en-GB/{CUSTOM}",
                is_custom=True,
            )
        ],
        customer_recipe_ids=[CUSTOM],
    )


def service_for(api):
    return lambda: SimpleNamespace(client=AsyncMock(return_value=api))


@pytest.mark.asyncio
async def test_read_calendar_week_includes_custom_name():
    api = SimpleNamespace(
        get_recipes_in_calendar_week=AsyncMock(return_value=[day_with_custom()])
    )
    result = await meal_planner.get_week(
        DATE, ensure_credentials=lambda: None, get_service=service_for(api),
    )
    parsed = json.loads(result)
    assert parsed["days"][0]["recipes"][0]["name"].startswith("Chicken Madras")
    api.get_recipes_in_calendar_week.assert_awaited_once_with(
        meal_planner._parse_day(DATE), resolve_custom_recipes=True
    )


@pytest.mark.asyncio
async def test_schedule_custom_recipe_and_verify():
    api = SimpleNamespace(
        get_recipes_in_calendar_week=AsyncMock(
            side_effect=[[], [day_with_custom()]]
        ),
        get_custom_recipe=AsyncMock(return_value=object()),
        add_custom_recipes_to_calendar=AsyncMock(),
    )
    result = await meal_planner.schedule(
        DATE, CUSTOM, True, ensure_credentials=lambda: None,
        get_service=service_for(api),
    )
    assert json.loads(result)["status"] == "scheduled"
    api.get_custom_recipe.assert_awaited_once_with(CUSTOM)
    api.add_custom_recipes_to_calendar.assert_awaited_once_with(
        meal_planner._parse_day(DATE), [CUSTOM]
    )


@pytest.mark.asyncio
async def test_repeated_schedule_does_not_write():
    api = SimpleNamespace(
        get_recipes_in_calendar_week=AsyncMock(return_value=[day_with_custom()]),
        add_custom_recipes_to_calendar=AsyncMock(),
        get_custom_recipe=AsyncMock(),
    )
    result = await meal_planner.schedule(
        DATE, CUSTOM, True, ensure_credentials=lambda: None,
        get_service=service_for(api),
    )
    assert json.loads(result)["status"] == "already_scheduled"
    api.add_custom_recipes_to_calendar.assert_not_awaited()
    api.get_custom_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalogue_scheduling_uses_catalogue_endpoint():
    api = SimpleNamespace(
        get_recipes_in_calendar_week=AsyncMock(side_effect=[
            [],
            [CookidooCalendarDay(
                id=DATE, title=DATE,
                recipes=[CookidooCalendarDayRecipe(
                    id="r123", name="Soup", total_time=1200,
                    thumbnail=None, image=None, url="https://cookidoo.co.uk/",
                )],
            )],
        ]),
        get_recipe_details=AsyncMock(),
        add_recipes_to_calendar=AsyncMock(),
    )
    result = await meal_planner.schedule(
        DATE, "r123", False, ensure_credentials=lambda: None,
        get_service=service_for(api),
    )
    assert json.loads(result)["status"] == "scheduled"
    api.add_recipes_to_calendar.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_write_failure_requests_read_before_retry():
    api = SimpleNamespace(
        get_recipes_in_calendar_week=AsyncMock(return_value=[]),
        get_custom_recipe=AsyncMock(),
        add_custom_recipes_to_calendar=AsyncMock(side_effect=TimeoutError()),
    )
    with pytest.raises(ToolError, match="Check My Week before retrying"):
        await meal_planner.schedule(
            DATE, CUSTOM, True, ensure_credentials=lambda: None,
            get_service=service_for(api),
        )


@pytest.mark.asyncio
async def test_invalid_dates_rejected_without_network():
    for invalid in ["tomorrow", "2099-02-30", "2099-W47-6"]:
        with pytest.raises(ToolError):
            await meal_planner.get_week(
                invalid, ensure_credentials=lambda: None,
                get_service=lambda: None,
            )


@pytest.mark.asyncio
async def test_planner_write_tool_gated_independently(monkeypatch):
    monkeypatch.delenv("COOKIDOO_MEAL_PLANNING_WRITES_ENABLED", raising=False)
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    async with Client(app.build_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "get_calendar_week" in names
    assert "schedule_recipe" not in names

    monkeypatch.setenv("COOKIDOO_MEAL_PLANNING_WRITES_ENABLED", "true")
    monkeypatch.delenv("COOKIDOO_RECIPE_CREATION_ENABLED", raising=False)
    async with Client(app.build_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "schedule_recipe" in names
    assert "upload_custom_recipe" not in names
