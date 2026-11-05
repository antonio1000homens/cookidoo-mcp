"""Allowlisted Cookidoo MCP transport for AWS Lambda.

The pinned upstream registers unsafe mutation tools by default. Instead,
construct a new server with an explicit read allowlist and separately gated,
reviewable recipe creation. Updating/deleting existing recipes stays disabled.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

import server as upstream
from schemas import Step

from src.auth import AccessGate, AccessVerifier
from src.config import ensure_credentials
from src.recipe_creation import create, preview
from src.recipe_images import preview as preview_image, upload as upload_image
from src.recipe_image_copy import preview as preview_image_copy, copy as copy_image

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}


async def _call(operation, **kwargs) -> str:
    try:
        await asyncio.to_thread(ensure_credentials)
        return await operation(**kwargs)
    except Exception:
        # Upstream wraps raw network exception messages in ToolError. Never
        # expose raw Cookidoo authentication errors/tokens to an MCP client.
        raise ToolError("Cookidoo operation failed; check service configuration") from None


def build_mcp() -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if upstream._cookidoo_service is not None:
                await upstream._cookidoo_service.close()

    mcp = FastMCP(
        "cookidoo-mcp-readonly",
        version=upstream.VERSION,
        instructions=(
            "Search and inspect Cookidoo recipes and prepare validated recipes. "
            "Creating a custom recipe requires a validated full preview, explicit user approval, "
            "an exact-content confirmation, and server-side write enablement. "
            "Never create from a preview alone; never update or delete existing recipes."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(annotations=READ_ONLY)
    async def search_recipes(
        query: str = "",
        ingredients: str = "",
        max_total_time: int | None = None,
        max_results: int = 10,
    ) -> str:
        """Search the Cookidoo catalogue by words, ingredients or time."""
        if not query and not ingredients:
            raise ToolError("Provide a query or ingredients")
        return await _call(
            upstream.search_recipes,
            query=query,
            ingredients=ingredients,
            max_total_time=max_total_time,
            max_results=max_results,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_recipe_details(recipe_id: str) -> str:
        """Return ingredients and steps for a catalogue recipe."""
        return await _call(upstream.get_recipe_details, recipe_id=recipe_id)

    @mcp.tool(annotations=READ_ONLY)
    async def list_custom_recipes() -> str:
        """List custom recipes in the authenticated Cookidoo account."""
        return await _call(upstream.list_custom_recipes)

    @mcp.tool(annotations=READ_ONLY)
    async def get_custom_recipe(recipe_id: str) -> str:
        """Read a custom recipe including editable structured guided settings."""
        return await _call(upstream.get_custom_recipe, recipe_id=recipe_id)

    @mcp.tool(annotations=READ_ONLY)
    async def generate_recipe_structure(
        name: str,
        ingredients: list[str] | str,
        steps: list[str | Step] | str,
        servings: int = 4,
        prep_time: int = 30,
        total_time: int = 60,
        hints: list[str] | str = "",
    ) -> str:
        """Validate and preview recipe JSON; does not upload it."""
        try:
            return await upstream.generate_recipe_structure(
                name=name,
                ingredients=ingredients,
                steps=steps,
                servings=servings,
                prep_time=prep_time,
                total_time=total_time,
                hints=hints,
            )
        except ToolError:
            raise
        except Exception:
            raise ToolError("Recipe validation failed") from None

    @mcp.tool(annotations=READ_ONLY)
    async def preview_custom_recipe_upload(recipe_json: str) -> str:
        """Preview a validated recipe and obtain its exact-content confirmation.

        This tool makes no Cookidoo writes. Show the full recipe to the user
        and ask for explicit approval before any creation tool call.
        """
        return preview(recipe_json)

    @mcp.tool(annotations=READ_ONLY)
    async def preview_custom_recipe_image(recipe_id: str, image_url: str) -> str:
        """Preview an image for one of your Cookidoo recipes. Does not change it.

        Only public HTTPS JPEG/PNG images are supported. This checks recipe
        ownership, decodes and sanitizes the image, then returns its exact
        content confirmation including the recipe's existing image state.
        Display the intended image to the user and obtain their explicit
        approval before calling set_custom_recipe_image.
        """
        return await preview_image(
            recipe_id, image_url,
            ensure_credentials=ensure_credentials, get_service=upstream._get_service,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def preview_copy_custom_recipe_image(
        source_recipe_id: str, destination_recipe_id: str,
    ) -> str:
        """Preview copying the existing picture from one owned recipe to another.

        The source recipe and its photo remain untouched. Requires both
        recipes to be owned by the signed-in Cookidoo user. The photo must
        be available on a public HTTPS Cookidoo CDN URL. Read-only; returns
        exact confirmation bound to source, destination, normalized picture
        content, and destination's current picture, for explicit user review.
        """
        return await preview_image_copy(
            source_recipe_id, destination_recipe_id,
            ensure_credentials=ensure_credentials,
            get_service=upstream._get_service,
        )

    # Never advertise a mutation tool until explicitly enabled by the server
    # operator in a separately reviewed production configuration change.
    if os.environ.get("COOKIDOO_RECIPE_CREATION_ENABLED") == "true":
        @mcp.tool(annotations=WRITE)
        async def upload_custom_recipe(recipe_json: str, confirmation: str) -> str:
            """Create one approved Cookidoo custom recipe.

            First call preview_custom_recipe_upload with the exact recipe JSON,
            show it to the user and obtain their explicit approval. Then use the
            identical recipe_json plus the confirmation from that preview.
            Existing recipe names and attempted uploads are fail-closed against
            duplicates. Never retry an ambiguous error automatically.
            """
            return await create(
                recipe_json, confirmation,
                ensure_credentials=ensure_credentials,
                get_service=upstream._get_service,
            )

        @mcp.tool(annotations=WRITE)
        async def set_custom_recipe_image(
            recipe_id: str, image_url: str, confirmation: str,
            replace_existing: bool = False,
        ) -> str:
            """Attach a reviewed JPEG/PNG photo to one of your custom recipes.

            First call preview_custom_recipe_image using exactly the same
            image_url and recipe_id. Show the photo and replacement status
            to the user and obtain their explicit permission. The exact
            confirmation binds the image bytes and prior recipe photo.
            Set replace_existing=true only with explicit user approval.
            No existing recipe text, ingredients, timing or steps change.
            Never retry an ambiguous upload failure automatically.
            """
            return await upload_image(
                recipe_id, image_url, confirmation, replace_existing,
                ensure_credentials=ensure_credentials, get_service=upstream._get_service,
            )

        @mcp.tool(annotations=WRITE)
        async def copy_custom_recipe_image(
            source_recipe_id: str, destination_recipe_id: str,
            confirmation: str, replace_existing: bool = False,
        ) -> str:
            """Copy an existing photo to a different one of your custom recipes.

            First call preview_copy_custom_recipe_image, show the source photo,
            source/destination names and whether the destination photo will be
            replaced, then obtain explicit approval from the user. Confirm
            exact preview contents and use replace_existing=true only if
            replacement was explicitly approved. A fresh Cookidoo-signed
            upload is made; the original photo and both recipes' ingredients,
            steps, and guided-cooking settings remain unchanged.
            """
            return await copy_image(
                source_recipe_id, destination_recipe_id, confirmation,
                replace_existing, ensure_credentials=ensure_credentials,
                get_service=upstream._get_service,
            )

    return mcp


def create_app():
    """Uvicorn factory: fail at startup unless Access JWT checks are configured."""
    verifier = AccessVerifier.from_environment()
    server = build_mcp()
    return AccessGate(
        server.http_app(path="/mcp", stateless_http=True, json_response=True),
        verifier,
    )
