"""Reviewed, SSRF-resistant photo updates for existing Cookidoo custom recipes.

Photo upload uses Cookidoo's signed Cloudinary flow. No images are modified
during preview. The caller must approve the *normalized pixels* and the
recipe's prior image state; a changed source or an intervening manual edit
invalidates that confirmation.

Only JPEG/PNG public HTTPS URLs are accepted. No local files, redirects, IP
literals, cookies, caller headers, or arbitrary network destinations.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import io
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urlsplit, quote

from aiohttp import ClientSession, ClientTimeout, DefaultResolver, FormData, TCPConnector
from aiohttp.abc import AbstractResolver
from botocore.exceptions import ClientError
from fastmcp.exceptions import ToolError
from PIL import Image, ImageOps, UnidentifiedImageError

from src.recipe_creation import GUARD_PREFIX

# Independently documented reverse-engineered Cookidoo image flow:
# https://github.com/Poket-Jony/cookidough-mcp (MIT-licensed project)
CLOUDINARY_URL = "https://api-eu.cloudinary.com/v1_1/vorwerk-users-gc/image/upload"
CLOUDINARY_PRESET = "prod-customer-recipe-signed"
CLOUDINARY_SOURCE = "uw"
CLOUDINARY_API_KEY = "993585863591145"  # Public Cloudinary tenant identifier, NOT a secret.

MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 6000
MIN_SIDE = 80
CONFIRM_PREFIX = "SET-COOKIDOO-IMAGE-SHA256:"
RECIPE_ID = re.compile(r"[a-zA-Z0-9_-]{8,64}\Z")
PUBLIC_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


@dataclass(frozen=True)
class PreparedImage:
    content: bytes
    mime: str
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _validate_url(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or len(raw) > 2048 or any(c.isspace() for c in raw):
        raise ToolError("Provide one public HTTPS image URL (up to 2048 characters)")
    try:
        u = urlsplit(raw)
        host = (u.hostname or "").lower().rstrip(".")
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            raise ValueError("Not a public HTTPS URL")
        if not host or "." not in host or not PUBLIC_HOST.fullmatch(host):
            raise ValueError("Not a public DNS hostname")
        if host.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".example")):
            raise ValueError("Local DNS hostname")
        if host.startswith(("localhost.", "metadata.")) or ".." in host:
            raise ValueError("Unsafe hostname")
        # Literal IPs, including public ones, must not bypass resolver checks.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP address literals not supported")
        if u.fragment:
            raise ValueError("Fragments unsupported")
    except (ValueError, TypeError):
        raise ToolError("Image URL must be an HTTPS address on a public DNS hostname") from None
    return raw, host


class _PublicResolver(AbstractResolver):
    """Validate the actual IPs aiohttp will connect to, not a separate DNS lookup."""

    def __init__(self):
        self.resolver = DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self.resolver.resolve(host, port, family)
        if not results or any(
            not ipaddress.ip_address(entry["host"]).is_global for entry in results
        ):
            raise OSError("Image URL resolved to a non-public address")
        return results

    async def close(self):
        await self.resolver.close()


def _normalize_image(data: bytes) -> PreparedImage:
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ToolError("Image is empty or larger than 10 MB")
    try:
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in ("JPEG", "PNG"):
                raise ValueError("Unsupported image format")
            w, h = original.size
            if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE and w*h <= MAX_PIXELS):
                raise ValueError("Image dimensions outside supported range")
            # Applying EXIF orientation then re-encoding strips embedded metadata
            # including GPS details before upload to Cookidoo/Cloudinary.
            normalized = ImageOps.exif_transpose(original)
            normalized.load()
            out = io.BytesIO()
            if original.format == "JPEG":
                normalized.convert("RGB").save(out, format="JPEG", quality=86, optimize=True)
                mime = "image/jpeg"
            else:
                normalized.save(out, format="PNG", optimize=True)
                mime = "image/png"
            processed = out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ToolError("Image must be a valid JPEG/PNG between 80 and 6000 px per side") from None
    if len(processed) > MAX_SOURCE_BYTES:
        raise ToolError("Normalized image exceeds Cookidoo's 10 MB limit")
    return PreparedImage(processed, mime, normalized.width, normalized.height)


async def _download_image(image_url: str) -> PreparedImage:
    url, _ = _validate_url(image_url)
    # New session each call, no proxy environment, redirects, cookies, DNS cache
    # or caller-controlled headers. IP validation occurs on the connect path.
    resolver = _PublicResolver()
    connector = TCPConnector(resolver=resolver, use_dns_cache=False)
    try:
        async with ClientSession(
            connector=connector, trust_env=False,
            timeout=ClientTimeout(total=12, sock_connect=4, sock_read=6)
        ) as session:
            async with session.get(url, allow_redirects=False,
                                   headers={"Accept": "image/jpeg, image/png"}) as resp:
                if resp.status != 200:
                    raise ToolError("Image URL did not return HTTP 200 (redirects are unsupported)")
                if resp.content_length is not None and resp.content_length > MAX_SOURCE_BYTES:
                    raise ToolError("Image exceeds 10 MB")
                payload = bytearray()
                async for part in resp.content.iter_chunked(64*1024):
                    payload.extend(part)
                    if len(payload) > MAX_SOURCE_BYTES:
                        raise ToolError("Image exceeds 10 MB")
    except ToolError:
        raise
    except Exception:
        raise ToolError("Could not fetch image from public HTTPS URL") from None
    return _normalize_image(bytes(payload))


async def _recipe_state(recipe_id: str, ensure_credentials, get_service):
    if not isinstance(recipe_id, str) or not RECIPE_ID.fullmatch(recipe_id):
        raise ToolError("Invalid Cookidoo custom recipe ID")
    try:
        await asyncio.to_thread(ensure_credentials)
        service = get_service()
        owned = await service.list_custom_recipes()
        if not any(str(item.id) == recipe_id for item in owned):
            raise ToolError("Recipe ID is not among your own custom recipes")
        recipe, _steps = await service.get_custom_recipe(recipe_id)
    except ToolError:
        raise
    except Exception:
        raise ToolError("Could not confirm ownership and current picture; no changes made") from None
    return service, str(recipe.name), str(getattr(recipe, "image", None) or "")


def _confirmation(recipe_id: str, image: PreparedImage, old_image: str) -> str:
    # Bind confirmation to exact image bytes, recipe ID and its prior image.
    old_hash = hashlib.sha256(old_image.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(
        (recipe_id + ":" + image.sha256 + ":" + old_hash).encode("ascii")
    ).hexdigest()
    return CONFIRM_PREFIX + digest


async def preview(recipe_id: str, image_url: str, *, ensure_credentials, get_service,
                  download_image=_download_image) -> str:
    url, host = _validate_url(image_url)
    service, title, old_image = await _recipe_state(recipe_id, ensure_credentials, get_service)
    del service
    image = await download_image(url)
    return json.dumps({
        "action": "PREVIEW_ONLY",
        "recipe_id": recipe_id,
        "recipe_name": title,
        "image_host": host,
        "mime": image.mime,
        "width": image.width,
        "height": image.height,
        "bytes": len(image.content),
        "image_sha256": image.sha256,
        "will_replace_existing_picture": bool(old_image),
        "metadata": "EXIF and embedded metadata stripped",
        "confirmation": _confirmation(recipe_id, image, old_image),
        "next_step": (
            "Show the intended image and recipe name to the user. Obtain their explicit "
            "approval before calling set_custom_recipe_image with the exact same recipe_id, "
            "image_url and confirmation. Replacing an existing photo requires replace_existing=true."
        ),
    })


def _reserve_image(bucket: str, recipe_id: str, image: PreparedImage) -> None:
    if not bucket.startswith("aws2022-lambda-code"):
        raise ToolError("Cookidoo image upload reservation is not configured")
    # A conditional write prevents duplicate outbound uploads on retries.
    marker = hashlib.sha256((recipe_id + ":" + image.sha256).encode()).hexdigest()
    try:
        import boto3
        boto3.client("s3").put_object(
            Bucket=bucket, Key=GUARD_PREFIX + "images/" + marker,
            Body=image.sha256.encode("ascii"),
            ContentType="text/plain", ServerSideEncryption="AES256", IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in (
            "PreconditionFailed", "ConditionalRequestConflict", "412", "409"
        ):
            raise ToolError(
                "This image was already submitted for this recipe. Check Cookidoo; "
                "do not automatically retry or clear the write guard."
            ) from None
        raise ToolError("Could not reserve image operation; nothing uploaded") from None
    except Exception:
        raise ToolError("Could not reserve image operation; nothing uploaded") from None


async def _cookidoo_image_update(service, recipe_id: str, image: PreparedImage) -> str:
    """Signed Vorwerk Cloudinary upload, then PATCH only the image property."""
    from yarl import URL

    api = await service.client()
    origin = api.api_endpoint
    language = api.localization.language
    if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", language):
        raise ToolError("Cookidoo image uploads are not supported for this locale")
    timestamp = int(time.time())
    signature_url = origin / ("created-recipes/" + language + "/image/signature")
    signature_body = {
        "timestamp": timestamp,
        "upload_preset": CLOUDINARY_PRESET,
        "source": CLOUDINARY_SOURCE,
    }
    try:
        signed = await api._request_json(
            "post", signature_url, "sign recipe image upload", json=signature_body
        )
        signature = signed.get("signature") if isinstance(signed, dict) else None
        if not isinstance(signature, str) or not signature:
            raise ValueError("Missing signature")
    except Exception:
        raise ToolError("Cookidoo did not sign the image upload; no image uploaded") from None

    form = FormData()
    for key, value in {
        "upload_preset": CLOUDINARY_PRESET, "source": CLOUDINARY_SOURCE,
        "timestamp": str(timestamp), "signature": signature, "api_key": CLOUDINARY_API_KEY
    }.items():
        form.add_field(key, value)
    form.add_field("file", image.content,
                   filename="photo.png" if image.mime == "image/png" else "photo.jpg",
                   content_type=image.mime)
    try:
        # NEVER forward Cookidoo cookies or access tokens to Cloudinary.
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            async with session.post(
                CLOUDINARY_URL, data=form, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise ValueError("Cloudinary upload did not complete")
                reply = await response.json(content_type=None)
        public_id = reply.get("public_id") if isinstance(reply, dict) else None
        fmt = reply.get("format") if isinstance(reply, dict) else None
        if not isinstance(public_id, str) or not re.fullmatch(r"[a-zA-Z0-9_/-]{1,256}", public_id):
            raise ValueError("Unexpected Cloudinary public_id")
        if fmt not in ("jpg", "jpeg", "png"):
            raise ValueError("Unexpected Cloudinary file format")
    except Exception:
        raise ToolError(
            "Image transfer to Cookidoo's media provider could not be confirmed. "
            "Do not retry automatically; verify the photo and write-guard first."
        ) from None

    # Do not PATCH any ingredients, steps, timings, or recipe title.
    patch_url = origin / ("created-recipes/" + language + "/" + quote(recipe_id, safe=""))
    try:
        await api._request_json("patch", patch_url, "attach custom recipe image",
                                json={"image": public_id + "." + fmt,
                                      "isImageOwnedByUser": True})
    except Exception:
        raise ToolError(
            "Photo was transferred but the Cookidoo recipe update is uncertain. "
            "Inspect the recipe before any retry; no retry was attempted."
        ) from None
    return public_id + "." + fmt


async def upload(recipe_id: str, image_url: str, confirmation: str,
                 replace_existing: bool = False, *, ensure_credentials,
                 get_service, download_image=_download_image,
                 update_image=_cookidoo_image_update) -> str:
    if os.environ.get("COOKIDOO_RECIPE_CREATION_ENABLED") != "true":
        raise ToolError("Cookidoo recipe writes are disabled by the server operator")
    url, _ = _validate_url(image_url)
    service, title, old_image = await _recipe_state(recipe_id, ensure_credentials, get_service)
    if old_image and not replace_existing:
        raise ToolError("This recipe already has a photo; explicitly approve replacing it")
    image = await download_image(url)
    if confirmation != _confirmation(recipe_id, image, old_image):
        raise ToolError(
            "Image, recipe or previous photo changed since preview. "
            "Review the photo again and obtain new explicit approval."
        )
    bucket = os.environ.get("COOKIDOO_WRITE_GUARD_BUCKET", "")
    await asyncio.to_thread(_reserve_image, bucket, recipe_id, image)
    image_ref = await update_image(service, recipe_id, image)
    try:
        details, _ = await service.get_custom_recipe(recipe_id)
        persisted = str(getattr(details, "image", None) or "")
        if not persisted or image_ref.removesuffix(".jpg").removesuffix(".jpeg").removesuffix(".png") not in persisted:
            raise ValueError("Persisted image not visible")
    except Exception:
        raise ToolError(
            "Cookidoo image update was sent but read-back verification failed. "
            "Check the recipe manually and do not retry automatically."
        ) from None
    return json.dumps({
        "updated": True, "recipe_id": recipe_id, "name": title,
        "url": "https://cookidoo.co.uk/created-recipes/en-GB/" + recipe_id,
        "verified": True, "image_sha256": image.sha256
    })
