"""Review-first reuse of an existing recipe's picture on another owned recipe.

Cookidoo's media provider does not allow reattaching the same original asset
reference to another recipe. We download the source image from its Cookidoo
CDN and create a *new* signed upload for the destination without changing
the source recipe or its settings.
"""
from __future__ import annotations

import hashlib
import json
import os

from fastmcp.exceptions import ToolError

from src import recipe_images as images

CONFIRM_PREFIX = "COPY-COOKIDOO-IMAGE-SHA256:"


def _token(source_id: str, destination_id: str, image: images.PreparedImage,
           old_destination_image: str) -> str:
    source = ":".join((
        "copy-image-v1", source_id, destination_id, image.sha256,
        hashlib.sha256(old_destination_image.encode("utf-8")).hexdigest(),
    ))
    return CONFIRM_PREFIX + hashlib.sha256(source.encode("ascii")).hexdigest()


async def _prepare(source_id: str, destination_id: str, *, ensure_credentials,
                   get_service, download_image=images._download_image):
    if source_id == destination_id:
        raise ToolError("Source and destination custom recipes must differ")
    service, source_name, source_url = await images._recipe_state(
        source_id, ensure_credentials, get_service)
    _service, destination_name, destination_url = await images._recipe_state(
        destination_id, ensure_credentials, get_service)
    if not source_url:
        raise ToolError("Source recipe has no picture to copy")
    # Accept only a publicly fetchable HTTPS source provided by the authenticated
    # Cookidoo API; do not expose its URL or credentials to the MCP caller.
    images._validate_url(source_url)
    prepared = await download_image(source_url)
    return (source_name, source_url, destination_name, destination_url, prepared)


async def preview(source_recipe_id: str, destination_recipe_id: str, *,
                  ensure_credentials, get_service,
                  download_image=images._download_image) -> str:
    source_name, _url, dest_name, old_dest_image, image = await _prepare(
        source_recipe_id, destination_recipe_id,
        ensure_credentials=ensure_credentials, get_service=get_service,
        download_image=download_image,
    )
    return json.dumps({
        "action": "PREVIEW_ONLY",
        "source_recipe_id": source_recipe_id,
        "source_recipe_name": source_name,
        "destination_recipe_id": destination_recipe_id,
        "destination_recipe_name": dest_name,
        "will_replace_existing_picture": bool(old_dest_image),
        "image_sha256": image.sha256,
        "mime": image.mime,
        "width": image.width,
        "height": image.height,
        "bytes": len(image.content),
        "confirmation": _token(
            source_recipe_id, destination_recipe_id, image, old_dest_image),
        "next_step": (
            "Show source/destination names and image to the user and request explicit approval. "
            "Call copy_custom_recipe_image with these exact IDs and confirmation only after approval. "
            "If the destination already has an image, replacement requires replace_existing=true."
        ),
    })


async def copy(source_recipe_id: str, destination_recipe_id: str,
               confirmation: str, replace_existing: bool = False,
               *, ensure_credentials, get_service,
               download_image=images._download_image,
               upload_image=images.upload) -> str:
    if os.environ.get("COOKIDOO_RECIPE_CREATION_ENABLED") != "true":
        raise ToolError("Cookidoo recipe image writes are disabled by the server operator")
    source_name, source_url, dest_name, old_dest_image, image = await _prepare(
        source_recipe_id, destination_recipe_id,
        ensure_credentials=ensure_credentials, get_service=get_service,
        download_image=download_image,
    )
    if old_dest_image and not replace_existing:
        raise ToolError("Destination already has a photo; replacement requires explicit approval")
    if confirmation != _token(
        source_recipe_id, destination_recipe_id, image, old_dest_image
    ):
        raise ToolError(
            "Picture or source/destination recipe state changed since preview; "
            "review again before uploading"
        )

    # Reuse the exact existing, independently fail-closed image upload path.
    # It downloads again and verifies the content and destination photo
    # state against this fresh confirmation before signing/attaching.
    result = await upload_image(
        destination_recipe_id, source_url,
        images._confirmation(destination_recipe_id, image, old_dest_image),
        replace_existing, ensure_credentials=ensure_credentials,
        get_service=get_service,
    )
    try:
        response = json.loads(result)
        if not response.get("updated") or not response.get("verified"):
            raise ValueError("Image not verified")
    except (ValueError, TypeError):
        raise ToolError(
            "Cookidoo may have updated the destination picture; inspect it "
            "before attempting another upload"
        ) from None
    return json.dumps({
        "copied": True,
        "source_recipe_id": source_recipe_id,
        "source_recipe_name": source_name,
        "destination_recipe_id": destination_recipe_id,
        "destination_recipe_name": dest_name,
        "source_unchanged": True,
        "verified": True,
        "url": response["url"],
        "image_sha256": image.sha256,
    })
