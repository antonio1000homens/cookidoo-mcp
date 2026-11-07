"""Guarded, in-place updates of existing Cookidoo custom recipes.

A preview binds the complete proposed recipe to the current remote recipe ID
and a fingerprint of the source (including its photo and raw guided steps).
No writes occur in preview. Actual edits are disabled by default, require
exact-content approval, and fail closed when the source changes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

from fastmcp.exceptions import ToolError
from guided import from_raw
from schemas import Step

from src.recipe_creation import validate_recipe

CONFIRM_PREFIX = "UPDATE-COOKIDOO-SHA256:"
RECIPE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _snapshot(recipe, instructions: list) -> dict:
    """Fingerprint the current remote content, not just the visible title."""
    return {
        "id": str(recipe.id),
        "name": recipe.name,
        "ingredients": recipe.ingredients,
        "instructions": instructions,  # Raw JSON retains original annotations.
        "servings": recipe.serving_size,
        "active_time": recipe.active_time,
        "total_time": recipe.total_time,
        "hints": recipe.hints,
        "tools": recipe.tools,
        "unit_text": getattr(recipe, "unit_text", None),
        "image": recipe.image,
        "image_owned_by_user": getattr(recipe, "image_owned_by_user", None),
        "work_status": getattr(recipe, "work_status", None),
        "requires_annotations_check": getattr(
            recipe, "requires_annotations_check", None
        ),
    }


def _confirmation(recipe_id: str, previous: str, proposed: str) -> str:
    return CONFIRM_PREFIX + _hash({
        "recipe_id": recipe_id,
        "original_sha256": previous,
        "proposed_sha256": proposed,
    })


async def _read_owned(recipe_id: str, ensure_credentials, get_service):
    if not isinstance(recipe_id, str) or not RECIPE_ID.fullmatch(recipe_id):
        raise ToolError("Invalid Cookidoo custom recipe ID")
    try:
        await asyncio.to_thread(ensure_credentials)
        service = get_service()
        owned = await service.list_custom_recipes()
        if not any(str(item.id) == recipe_id for item in owned):
            raise ToolError("Recipe is not owned by the authenticated Cookidoo account")
        recipe, instructions = await service.get_custom_recipe(recipe_id)
        if str(recipe.id) != recipe_id:
            raise ToolError("Cookidoo returned a different recipe ID")
        return service, recipe, instructions
    except ToolError:
        raise
    except Exception:
        raise ToolError(
            "Unable to inspect this Cookidoo recipe; no update was attempted"
        ) from None


def _semantic_step(value: object) -> object:
    if isinstance(value, Step):
        # Cookidoo may return a plain string for a step with no annotations.
        if not value.settings and not value.ingredients:
            return value.text
        return value.model_dump(mode="json", exclude_none=True)
    return value


async def preview(recipe_id: str, recipe_json: str, *,
                  ensure_credentials, get_service) -> str:
    recipe, proposed_sha, proposed = validate_recipe(recipe_json)
    _, current, raw_steps = await _read_owned(
        recipe_id, ensure_credentials, get_service
    )
    original_sha = _hash(_snapshot(current, raw_steps))
    return json.dumps({
        "action": "PREVIEW_ONLY",
        "recipe_id": recipe_id,
        "original_name": current.name,
        "new_name": recipe.name,
        "original_sha256": original_sha,
        "proposed_sha256": proposed_sha,
        "replaces_existing_recipe": True,
        "original_steps": len(raw_steps),
        "new_steps": len(recipe.steps),
        "original_ingredients": len(current.ingredients),
        "new_ingredients": len(recipe.ingredients),
        "existing_image_present": bool(current.image),
        "image_action": "Preserve the current recipe photo; no image upload",
        "recipe": proposed,
        "confirmation": _confirmation(recipe_id, original_sha, proposed_sha),
        "next_step": (
            "Show the full proposed recipe and the original recipe ID/name, "
            "clearly explain that its existing contents will be replaced, "
            "and obtain explicit approval for this exact confirmation. "
            "Only then call update_custom_recipe with identical inputs."
        ),
    }, ensure_ascii=False, indent=2)


async def update(recipe_id: str, recipe_json: str, confirmation: str, *,
                 ensure_credentials, get_service) -> str:
    import os

    if os.environ.get("COOKIDOO_RECIPE_UPDATES_ENABLED") != "true":
        raise ToolError("Cookidoo recipe updates are disabled by the server operator")
    recipe, proposed_sha, proposed = validate_recipe(recipe_json)

    # Do not accept a malformed approval or access Cookidoo for an invalid ID.
    if not isinstance(recipe_id, str) or not RECIPE_ID.fullmatch(recipe_id):
        raise ToolError("Invalid Cookidoo custom recipe ID")
    if not isinstance(confirmation, str) or not confirmation.startswith(CONFIRM_PREFIX):
        raise ToolError("Missing exact-content approval; preview the update first")

    service, current, raw_steps = await _read_owned(
        recipe_id, ensure_credentials, get_service
    )
    original_sha = _hash(_snapshot(current, raw_steps))
    if confirmation != _confirmation(recipe_id, original_sha, proposed_sha):
        raise ToolError(
            "Approval does not match the current recipe and proposed changes. "
            "The recipe may have changed since preview; preview it again. "
            "No update was attempted."
        )

    old_image = current.image
    old_image_owned = getattr(current, "image_owned_by_user", None)
    try:
        # The pinned upstream Cookidoo API first fetches the current recipe,
        # merges fields, and rejects any existing owned photo it cannot retain.
        await service.update_custom_recipe(recipe_id, recipe)
    except Exception:
        raise ToolError(
            "Cookidoo update could not be confirmed. Do not retry automatically; "
            "retrieve the recipe to see whether it was updated."
        ) from None

    try:
        _, fresh, fresh_steps = await _read_owned(
            recipe_id, ensure_credentials, get_service
        )
        same_data = (
            fresh.name == proposed["name"]
            and fresh.ingredients == proposed["ingredients"]
            and fresh.serving_size == proposed["servings"]
            and fresh.active_time == proposed["prep_time"] * 60
            and fresh.total_time == proposed["total_time"] * 60
            and (fresh.hints or []) == (proposed.get("hints") or [])
        )
        actual_steps = [_semantic_step(from_raw(step)) for step in fresh_steps]
        proposed_steps = [_semantic_step(step) for step in recipe.steps]
        same_steps = actual_steps == proposed_steps
        same_image = (
            fresh.image == old_image
            and getattr(fresh, "image_owned_by_user", None) == old_image_owned
        )
        same_id = str(fresh.id) == recipe_id
    except Exception:
        raise ToolError(
            "Cookidoo accepted the update but verification failed. "
            "Do not retry automatically; inspect the recipe manually."
        ) from None

    if not (same_data and same_steps and same_image and same_id):
        raise ToolError(
            "Cookidoo update verification found a mismatch in recipe data, "
            "guided steps, ID or image. Do not retry automatically; inspect "
            "the recipe manually."
        )
    return json.dumps({
        "updated": True,
        "verified": True,
        "recipe_id": recipe_id,
        "name": fresh.name,
        "image_preserved": same_image,
        "guided_step_count": sum(isinstance(s, Step) for s in recipe.steps),
        "steps_count": len(recipe.steps),
        "url": fresh.url,
        "message": "Existing Cookidoo recipe updated and verified in place.",
    }, ensure_ascii=False)
