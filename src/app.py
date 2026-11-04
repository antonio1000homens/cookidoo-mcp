"""Read-only Cookidoo MCP transport for AWS Lambda.

The v1.1.0 upstream server registers mutation tools. Rather than hiding them
with a UI hint or relying on prompt instructions, construct a *new* MCP server
that registers only explicitly allowlisted read/validation tools.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

import server as upstream
from schemas import Step

from src.auth import AccessGate, AccessVerifier
from src.config import ensure_credentials

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}


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
            "This endpoint is read-only: it does not publish or edit recipes."
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

    return mcp


def create_app():
    """Uvicorn factory: fail at startup unless Access JWT checks are configured."""
    verifier = AccessVerifier.from_environment()
    server = build_mcp()
    return AccessGate(
        server.http_app(path="/mcp", stateless_http=True, json_response=True),
        verifier,
    )
