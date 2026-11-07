"""Offline checks for guarded, in-place Cookidoo recipe updates.

No live Cookidoo calls, credentials, or AWS infrastructure are involved.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import app, recipe_updates

OLD_ID = "01M6V1VNJHT22PZ1DXPXASDFR0"
IMAGE = "prod/img/customer-recipe/existing-photo.jpg"
RECIPE = {
    "name": "Chicken Madras TM7 - Browning Edition",
    "ingredients": ["500 g chicken thighs", "40 g refined rapeseed oil"],
    "steps": [
        "Weigh the chicken.",
        {"text": "Brown {settings}.", "settings": [
            {"mode": "browning", "time": 300, "temperature": "160", "power": "intense"}
        ]},
    ],
    "servings": 4,
    "prep_time": 30,
    "total_time": 80,
    "hints": ["Check the chicken reaches 75 C."],
}
JSON = json.dumps(RECIPE)
RAW_BROWN = {
    "text": "Brown Browning/5 min/160°C/intense.",
    "annotations": [
        {
            "type": "MODE",
            "name": "browning",
            "data": {
                "time": 300,
                "temperature": {"value": "160", "unit": "C"},
                "power": "Intense",
            },
            "position": {"offset": 6, "length": len("Browning/5 min/160°C/intense")},
        }
    ],
}


def current(name="Original Chicken Madras", image=IMAGE):
    return SimpleNamespace(
        id=OLD_ID, name=name, ingredients=["500 g chicken thighs", "40 g ghee"],
        serving_size=4, active_time=1800, total_time=4800,
        hints=["Original hint"], tools=["TM7"], image=image,
        image_owned_by_user=bool(image), work_status="PRIVATE",
        requires_annotations_check=False, unit_text="serving",
        url=f"https://cookidoo.co.uk/created-recipes/en-GB/{OLD_ID}",
    )


def after(image=IMAGE):
    updated = current(name=RECIPE["name"], image=image)
    updated.ingredients = RECIPE["ingredients"]
    updated.hints = RECIPE["hints"]
    return updated


def fake_service(*, old=None, steps=None, fresh=None, fresh_steps=None):
    old = old or current()
    steps = steps if steps is not None else ["Original guided step"]
    fresh = fresh or after()
    fresh_steps = fresh_steps if fresh_steps is not None else [
        "Weigh the chicken.", RAW_BROWN
    ]
    return SimpleNamespace(
        list_custom_recipes=AsyncMock(return_value=[SimpleNamespace(id=OLD_ID)]),
        get_custom_recipe=AsyncMock(side_effect=[
            (old, steps), (old, steps), (fresh, fresh_steps)
        ]),
        update_custom_recipe=AsyncMock(return_value=fresh),
    )


@pytest.mark.asyncio
async def test_preview_never_mutates_and_binds_id_image_source_and_new_content():
    svc = fake_service()
    item = json.loads(await recipe_updates.preview(
        OLD_ID, JSON, ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    assert item["action"] == "PREVIEW_ONLY"
    assert item["recipe_id"] == OLD_ID
    assert item["existing_image_present"] is True
    assert item["recipe"]["ingredients"] == RECIPE["ingredients"]
    assert item["recipe"]["steps"][1]["settings"][0]["mode"] == "browning"
    assert item["confirmation"].startswith(recipe_updates.CONFIRM_PREFIX)
    svc.update_custom_recipe.assert_not_awaited()
    assert svc.get_custom_recipe.await_count == 1


@pytest.mark.asyncio
async def test_updates_are_disabled_by_default_before_network(monkeypatch):
    monkeypatch.delenv("COOKIDOO_RECIPE_UPDATES_ENABLED", raising=False)
    with pytest.raises(ToolError, match="disabled"):
        await recipe_updates.update(OLD_ID, JSON, "bad",
            ensure_credentials=lambda: 1 / 0, get_service=lambda: 1 / 0)


@pytest.mark.asyncio
async def test_invalid_confirmation_is_rejected_before_cookidoo(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    with pytest.raises(ToolError, match="approval"):
        await recipe_updates.update(OLD_ID, JSON, "bad",
            ensure_credentials=lambda: 1 / 0, get_service=lambda: 1 / 0)


@pytest.mark.asyncio
async def test_stale_source_or_different_target_fails_without_patch(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    svc = fake_service()
    p = json.loads(await recipe_updates.preview(
        OLD_ID, JSON, ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    svc.get_custom_recipe = AsyncMock(return_value=(current("Changed remotely"), ["Original guided step"]))
    with pytest.raises(ToolError, match="may have changed"):
        await recipe_updates.update(
            OLD_ID, JSON, p["confirmation"],
            ensure_credentials=lambda: None, get_service=lambda: svc
        )
    svc.update_custom_recipe.assert_not_awaited()

    with pytest.raises(ToolError, match="owned"):
        await recipe_updates.preview(
            "DIFFERENT", JSON, ensure_credentials=lambda: None,
            get_service=lambda: svc
        )


@pytest.mark.asyncio
async def test_changed_proposal_does_not_match_approval(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    svc = fake_service()
    p = json.loads(await recipe_updates.preview(
        OLD_ID, JSON, ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    new = json.dumps({**RECIPE, "name": "Changed post approval"})
    with pytest.raises(ToolError, match="Approval does not match"):
        await recipe_updates.update(OLD_ID, new, p["confirmation"],
            ensure_credentials=lambda: None, get_service=lambda: svc)
    svc.update_custom_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_approval_updates_same_id_and_verifies_guided_steps_and_photo(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    svc = fake_service()
    p = json.loads(await recipe_updates.preview(
        OLD_ID, JSON, ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    result = json.loads(await recipe_updates.update(
        OLD_ID, JSON, p["confirmation"],
        ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    assert result["updated"] is True and result["verified"] is True
    assert result["recipe_id"] == OLD_ID
    assert result["image_preserved"] is True
    assert result["guided_step_count"] == 1
    svc.update_custom_recipe.assert_awaited_once()
    assert svc.update_custom_recipe.await_args.args[0] == OLD_ID
    assert svc.update_custom_recipe.await_args.args[1].ingredients == RECIPE["ingredients"]


@pytest.mark.asyncio
async def test_changed_photo_fails_verification_without_retry(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    svc = fake_service(fresh=after(image="prod/img/customer-recipe/other.jpg"))
    p = json.loads(await recipe_updates.preview(
        OLD_ID, JSON, ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    with pytest.raises(ToolError, match="verification found a mismatch"):
        await recipe_updates.update(
            OLD_ID, JSON, p["confirmation"],
            ensure_credentials=lambda: None, get_service=lambda: svc
        )
    svc.update_custom_recipe.assert_awaited_once()


@pytest.mark.asyncio
async def test_network_failure_is_ambiguous_and_is_not_retried(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    svc = fake_service()
    p = json.loads(await recipe_updates.preview(
        OLD_ID, JSON, ensure_credentials=lambda: None, get_service=lambda: svc
    ))
    svc.update_custom_recipe.side_effect = TimeoutError("secret material")
    with pytest.raises(ToolError, match="Do not retry automatically"):
        await recipe_updates.update(
            OLD_ID, JSON, p["confirmation"],
            ensure_credentials=lambda: None, get_service=lambda: svc
        )
    svc.update_custom_recipe.assert_awaited_once()


@pytest.mark.asyncio
async def test_catalog_has_update_preview_but_not_write_until_separately_enabled(monkeypatch):
    monkeypatch.delenv("COOKIDOO_RECIPE_UPDATES_ENABLED", raising=False)
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    async with Client(app.build_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "preview_custom_recipe_update" in names
    assert "update_custom_recipe" not in names
    assert "upload_custom_recipe" in names
    monkeypatch.setenv("COOKIDOO_RECIPE_UPDATES_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "false")
    async with Client(app.build_mcp()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "update_custom_recipe" in names
    assert "upload_custom_recipe" not in names
