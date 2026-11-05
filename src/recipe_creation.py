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
    return json.dumps(
        {
            "created": True,
            "recipe_id": str(created.id),
            "name": created.name,
            "url": created.url,
            "message": "Recipe created in Cookidoo. Retrieve it to verify guided settings.",
        },
        ensure_ascii=False,
    )
