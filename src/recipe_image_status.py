"""Read-only, evidence-based status of a custom recipe's actual stored picture.

A saved reference and a successful recipe edit are *not* evidence that the
picture is retrievable. The check never updates recipes or uploads media.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from fastmcp.exceptions import ToolError

from src import recipe_images as images


async def get_status(recipe_id: str, *, ensure_credentials, get_service,
                     download_image=None) -> str:
    """Inspect the stored image reference and fetch it when safely possible.

    A recipe that is accessible by ID but absent from the account's list is
    explicitly reported as such; it is NOT treated as verified ownership for
    the guarded image-upload operation.
    """
    if not isinstance(recipe_id, str) or not images.RECIPE_ID.fullmatch(recipe_id):
        raise ToolError("Invalid Cookidoo custom recipe ID")
    try:
        await asyncio.to_thread(ensure_credentials)
        service = get_service()
    except Exception:
        raise ToolError("Could not authenticate for read-only image inspection") from None

    list_status = "unavailable"
    try:
        recipes = await service.list_custom_recipes()
        list_status = ("present" if any(str(item.id) == recipe_id for item in recipes)
                       else "absent")
    except Exception:
        # A failed listing is not evidence that the recipe is absent.
        pass

    result = {
        "recipe_id": recipe_id,
        "read_only": True,
        "recipe_list_status": list_status,
        "ownership_verified_by_list": list_status == "present",
        "recipe_accessible_by_id": False,
        "image_reference": None,
        "image_reference_present": None,
        "image_status": "not_checked",
        "retrieval_attempted": False,
        "image_retrievable": None,
        "image_present_and_verified": False,
    }
    try:
        recipe, _steps = await service.get_custom_recipe(recipe_id)
        if str(recipe.id) != recipe_id:
            raise ValueError("Mismatched recipe ID")
    except Exception:
        result["next_step"] = (
            "The recipe could not be read by ID. Do not infer that it has no photo "
            "or retry any uncertain recipe creation/upload automatically."
        )
        return json.dumps(result, ensure_ascii=False)

    result["recipe_accessible_by_id"] = True
    reference = str(getattr(recipe, "image", None) or "")
    result["image_reference"] = reference or None
    result["image_reference_present"] = bool(reference)
    if not reference:
        result["image_status"] = "missing"
        result["image_retrievable"] = False
    else:
        try:
            images._validate_url(reference)
        except ToolError:
            # Some Cookidoo responses contain a media asset identifier rather
            # than an absolute URL. Never invent an image CDN URL.
            result["image_status"] = "reference_not_fetchable"
            result["next_step"] = (
                "A photo reference exists, but it is not a public HTTPS URL. "
                "Its retrievability cannot be established from this reference."
            )
        else:
            result["retrieval_attempted"] = True
            fetch = download_image or images._download_image
            try:
                prepared = await fetch(reference)
            except Exception:
                result["image_status"] = "unreachable"
                result["image_retrievable"] = False
                result["next_step"] = (
                    "An image reference is saved, but the photo could not be "
                    "retrieved and decoded. Do not report the image as verified."
                )
            else:
                result["image_status"] = "present_and_verified"
                result["image_retrievable"] = True
                result["image_present_and_verified"] = True
                result["image_sha256"] = prepared.sha256

    if list_status != "present":
        result["upload_eligibility"] = "not_verified_in_custom_recipe_list"
        result["next_step"] = (
            "The recipe is not confirmed in the custom-recipe list. "
            "Do not attempt an image write until list membership is confirmed; "
            "checking a recipe directly does not establish ownership for writes."
        )
    else:
        result["upload_eligibility"] = "account_recipe_confirmed"
    return json.dumps(result, ensure_ascii=False)
