"""Reviewable, duplicate-resistant Cookidoo custom recipe creation.

The existing S3 release bucket stores a create-only reservation per normalized
recipe title. This prevents retries or concurrent Lambda executions from
silently creating the same recipe twice without adding a database. A failed
upstream create intentionally leaves its reservation for manual inspection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Any

from botocore.exceptions import ClientError
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from schemas import CustomRecipe, Step

MAX_RECIPE_BYTES = 65_536
MAX_INGREDIENTS = 100
MAX_STEPS = 80
MAX_HINTS = 30
CONFIRM_PREFIX = "CREATE-COOKIDOO-SHA256:"
GUARD_PREFIX = "cookidoo-mcp/write-guards/"
_ADDITION_VERBS = re.compile(r"\b(?:add|put|place|weigh|pour|tip|combine)\b", re.IGNORECASE)


def review_ingredient_links(recipe: CustomRecipe) -> dict[str, Any]:
    """Verify explicit ingredient links; warn about likely omissions.

    Plain prose steps are not required to carry links. Missing links are
    warnings so older, otherwise-valid recipes remain backward compatible.
    """
    allowed = set(recipe.ingredients)
    linked_count = 0
    warnings: list[str] = []
    for number, step in enumerate(recipe.steps, start=1):
        if not isinstance(step, Step):
            continue
        links = step.model_dump(mode="json", exclude_none=True).get("ingredients") or []
        seen: set[str] = set()
        for link in links:
            reference, label = link["ingredient"], link["text"]
            if reference not in allowed:
                raise ToolError(
                    f"Step {number}: ingredient link must reference an exact "
                    "entry in the recipe ingredients list"
                )
            if not label or label not in step.text:
                raise ToolError(
                    f"Step {number}: ingredient link text must occur verbatim in step text"
                )
            if reference in seen:
                raise ToolError(
                    f"Step {number}: link each ingredient only once per guided step"
                )
            seen.add(reference)
            linked_count += 1
        if step.settings and not links and _ADDITION_VERBS.search(step.text):
            warnings.append(
                f"Step {number} adds ingredients but has no individual ingredient "
                "links; check steps[].ingredients before seeking approval"
            )
    return {
        "linked_ingredient_count": linked_count,
        "ingredient_link_warnings": warnings,
    }



def validate_recipe(recipe_json: str) -> tuple[CustomRecipe, str, dict[str, Any]]:
    """Validate the pinned upstream schema with tighter request-size bounds."""
    if not isinstance(recipe_json, str) or len(recipe_json.encode("utf-8")) > MAX_RECIPE_BYTES:
        raise ToolError("Recipe JSON must be a string no longer than 64 KiB")
    try:
        recipe = CustomRecipe.model_validate_json(recipe_json)
    except (ValidationError, ValueError):
        raise ToolError("Invalid recipe JSON; use generate_recipe_structure first") from None

    if len(recipe.ingredients) > MAX_INGREDIENTS or any(
        not value.strip() or len(value) > 250 for value in recipe.ingredients
    ):
        raise ToolError("Recipe ingredients exceed the supported limits")
    if len(recipe.steps) > MAX_STEPS or any(
        len(step if isinstance(step, str) else step.text) > 1000
        or not (step if isinstance(step, str) else step.text).strip()
        for step in recipe.steps
    ):
        raise ToolError("Recipe steps exceed the supported limits")
    if recipe.hints and (
        len(recipe.hints) > MAX_HINTS
        or any(not hint.strip() or len(hint) > 500 for hint in recipe.hints)
    ):
        raise ToolError("Recipe hints exceed the supported limits")
    review_ingredient_links(recipe)

    normalized = recipe.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return recipe, digest, normalized


def preview(recipe_json: str) -> str:
    recipe, digest, normalized = validate_recipe(recipe_json)
    return json.dumps(
        {
            "action": "PREVIEW_ONLY",
            "name": recipe.name,
            "servings": recipe.servings,
            "prep_time": recipe.prep_time,
            "total_time": recipe.total_time,
            "guided_step_count": sum(isinstance(s, Step) for s in recipe.steps),
            **review_ingredient_links(recipe),
            "ingredients_count": len(recipe.ingredients),
            "steps_count": len(recipe.steps),
            "recipe": normalized,
            "confirmation": CONFIRM_PREFIX + digest,
            "next_step": (
                "Show the full recipe to the user. Only after the user explicitly approves "
                "creating exactly this recipe, call upload_custom_recipe with the same "
                "recipe_json and this confirmation value. Do not upload on preview alone."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _reserve_title(bucket: str, recipe: CustomRecipe, digest: str) -> None:
    """Create-only S3 marker. No recipe text, personal data or credentials stored."""
    key = GUARD_PREFIX + hashlib.sha256(
        recipe.name.strip().casefold().encode("utf-8")
    ).hexdigest()
    try:
        import boto3

        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=digest.encode("ascii"),
            ContentType="text/plain",
            ServerSideEncryption="AES256",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            raise ToolError(
                "A recipe with this title has already been submitted. "
                "Check list_custom_recipes before attempting anything else. "
                "If the original upload failed, inspect and clear its S3 "
                "write-guard marker manually before retrying."
            ) from None
        raise ToolError("Recipe creation reservation failed; no upload was attempted") from None
    except Exception:
        raise ToolError("Recipe creation reservation failed; no upload was attempted") from None


async def create(recipe_json: str, confirmation: str, *,
                 ensure_credentials, get_service) -> str:
    """Explicit gated upload, with title preflight and conditional S3 reservation."""
    if os.environ.get("COOKIDOO_RECIPE_CREATION_ENABLED") != "true":
        raise ToolError("Cookidoo recipe creation is disabled by the server operator")
    recipe, digest, _ = validate_recipe(recipe_json)
    if confirmation != CONFIRM_PREFIX + digest:
        raise ToolError(
            "Recipe confirmation is missing or does not match the reviewed contents. "
            "Preview this exact recipe and obtain explicit user approval first."
        )
    bucket = os.environ.get("COOKIDOO_WRITE_GUARD_BUCKET", "")
    if not bucket or not bucket.startswith("aws2022-lambda-code"):
        # Restrict this feature to the already provisioned release bucket,
        # not an arbitrary caller-provided S3 destination.
        raise ToolError("Cookidoo write guard is not configured")

    try:
        await asyncio.to_thread(ensure_credentials)
        service = get_service()
        existing = await service.list_custom_recipes()
    except Exception:
        raise ToolError("Could not verify existing Cookidoo recipes; no upload attempted") from None

    if any(str(item.name).strip().casefold() == recipe.name.strip().casefold()
           for item in existing):
        raise ToolError(
            "A Cookidoo custom recipe with this title already exists; "
            "open it before creating another."
        )

    await asyncio.to_thread(_reserve_title, bucket, recipe, digest)
    try:
        created = await service.create_custom_recipe(recipe)
    except Exception:
        # IMPORTANT: retain marker on ambiguous upstream failure rather than
        # retrying and creating duplicate recipes.
        raise ToolError(
            "Cookidoo upload could not be confirmed. Do not retry automatically. "
            "Check list_custom_recipes and the write-guard reservation first."
        ) from None
    # Successful create responses do not guarantee immediate list visibility.
    # A missing entry may reflect eventual consistency; never retry creation.
    try:
        refreshed = await service.list_custom_recipes()
        list_status = ("present" if any(str(item.id) == str(created.id)
                                         for item in refreshed) else "absent")
    except Exception:
        list_status = "unavailable"

    return json.dumps(
        {
            "created": True,
            "recipe_id": str(created.id),
            "name": created.name,
            "url": created.url,
            "recipe_list_status": list_status,
            "image_status": "not_checked",
            "image_present_and_verified": None,
            "image_upload_status": "not_started",
            "image_upload_workflow": (
                "No image was uploaded. First use get_custom_recipe_image_status "
                "to inspect the saved image and confirm recipe_list_status=present. "
                "If a new photo is wanted, call preview_custom_recipe_image with "
                "the recipe ID and public HTTPS image URL; show the photo and "
                "obtain separate explicit user approval; then call "
                "set_custom_recipe_image with the exact confirmation and, "
                "finally, get_custom_recipe_image_status again. Do not claim "
                "image success until image_present_and_verified=true."
            ),
            "message": (
                "Cookidoo returned a new recipe ID; list visibility and the photo "
                "are separate checks. Do not repeat creation if listing is delayed."
            ),
        },
        ensure_ascii=False,
    )
