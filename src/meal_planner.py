"""Read and schedule Cookidoo My Week recipes using pinned cookidoo-api 0.19.

Only a single recipe is scheduled per call. Write access is separately gated
in src.app; none of these helpers are registered by themselves.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from datetime import date

from fastmcp.exceptions import ToolError


def _parse_day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ToolError("Date must be in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError("Invalid calendar date") from None


def _is_planned(days: list, day: date, recipe_id: str) -> bool:
    for entry in days:
        if entry.id != day.isoformat():
            continue
        return (
            recipe_id in (entry.customer_recipe_ids or [])
            or any(item.id == recipe_id for item in entry.recipes)
        )
    return False


async def get_week(day: str, *, ensure_credentials, get_service) -> str:
    """List the week containing the supplied day, including owned custom recipes."""
    target = _parse_day(day)
    try:
        await asyncio.to_thread(ensure_credentials)
        api = await get_service().client()
        days = await api.get_recipes_in_calendar_week(
            target, resolve_custom_recipes=True
        )
        return json.dumps(
            {"week_containing": target.isoformat(), "days": [asdict(item) for item in days]},
            ensure_ascii=False,
        )
    except Exception:
        raise ToolError("Unable to read Cookidoo My Week; check connection") from None


async def schedule(
    day: str, recipe_id: str, is_custom: bool = False, *,
    ensure_credentials, get_service,
) -> str:
    """Idempotently schedule one catalogue or custom recipe for a date."""
    target = _parse_day(day)
    if target < date.today():
        raise ToolError("Cannot schedule a recipe on a past date")
    if not isinstance(recipe_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", recipe_id):
        raise ToolError("Invalid recipe ID")
    if not is_custom and not re.fullmatch(r"r\d+", recipe_id):
        raise ToolError("This looks like a custom recipe ID: set is_custom=true")

    try:
        await asyncio.to_thread(ensure_credentials)
        service = get_service()
        api = await service.client()
        current = await api.get_recipes_in_calendar_week(target)
        if _is_planned(current, target, recipe_id):
            return json.dumps({
                "status": "already_scheduled",
                "day": target.isoformat(),
                "recipe_id": recipe_id,
                "is_custom": is_custom,
            })

        if is_custom:
            # Confirm the custom ID belongs to the authenticated account.
            await api.get_custom_recipe(recipe_id)
            await api.add_custom_recipes_to_calendar(target, [recipe_id])
        else:
            await api.get_recipe_details(recipe_id)
            await api.add_recipes_to_calendar(target, [recipe_id])

        verified = await api.get_recipes_in_calendar_week(target)
        if not _is_planned(verified, target, recipe_id):
            raise ToolError(
                "Cookidoo did not confirm the scheduled recipe. Check My Week before retrying."
            )
        return json.dumps({
            "status": "scheduled",
            "day": target.isoformat(),
            "recipe_id": recipe_id,
            "is_custom": is_custom,
        })
    except ToolError:
        raise
    except Exception:
        # A network failure after the write may have persisted the change.
        # Never suggest an automatic retry without first reading the calendar.
        raise ToolError(
            "Cookidoo scheduling outcome is unconfirmed. Check My Week before retrying."
        ) from None
