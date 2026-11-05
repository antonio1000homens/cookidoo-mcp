"""Photo upload is a separate, explicitly reviewed Cookidoo write workflow.

These tests use synthetic images, synthetic authentication and in-memory fake
Cookidoo/Cloudinary clients. They never mutate a real recipe or contact a URL.
"""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, PngImagePlugin
from fastmcp import Client
from fastmcp.exceptions import ToolError
from botocore.exceptions import ClientError

from src import app, recipe_images as images


def _sample(format="PNG", size=(120, 110), secret=None):
    raw = io.BytesIO()
    picture = Image.new("RGB", size, (190, 30, 40))
    if format == "PNG" and secret:
        info = PngImagePlugin.PngInfo()
        info.add_text("sensitive_context", secret)
        picture.save(raw, "PNG", pnginfo=info)
    else:
        picture.save(raw, format)
    return raw.getvalue()


def _prepared(color=(190, 30, 40)):
    b = io.BytesIO()
    Image.new("RGB", (120, 110), color).save(b, "PNG")
    return images._normalize_image(b.getvalue())


def _fake_service(recipe_id="01M6P47BB8PG942TKR31TZ9FFE", old_image=""):
    state = SimpleNamespace(
        id=recipe_id, name="Chicken Madras TM7", image=old_image
    )
    service = SimpleNamespace(
        list_custom_recipes=AsyncMock(return_value=[state]),
        get_custom_recipe=AsyncMock(side_effect=lambda _: (state, [])),
    )
    return service, state


def test_strips_private_metadata_and_keeps_valid_picture():
    data = _sample(secret="GPS: private address")
    picture = images._normalize_image(data)
    assert picture.mime == "image/png"
    assert picture.width == 120 and picture.height == 110
    assert b"private address" not in picture.content
    assert picture.sha256 == images._normalize_image(data).sha256
    jpg = images._normalize_image(_sample("JPEG"))
    assert jpg.mime == "image/jpeg"


@pytest.mark.parametrize("data", [
    b"", b"not a photo", _sample("PNG", (79, 160)),
    _sample("PNG", (6500, 100)),
    b"PNG fake",
])
def test_invalid_images_are_rejected(data):
    with pytest.raises(ToolError):
        images._normalize_image(data)


@pytest.mark.parametrize("url", [
    "http://public.example.org/photo.png", "file:///tmp/photo.png",
    "https://127.0.0.1/photo.png", "https://localhost/photo.jpg",
    "https://metadata.google.internal/secret.png",
    "https://foo.local/img.png", "https://me:pass@pictures.example.com/x.png",
    "https://public.example.org:1234/img.png",
    "https://x.example.com/#fragment",
    "https://[::1]/image.png",
    "https://public.example.org/pic.png\nAuthorization: fake",
])
def test_private_or_unsafe_image_sources_rejected(url):
    with pytest.raises(ToolError):
        images._validate_url(url)


@pytest.mark.asyncio
async def test_resolver_checks_actual_connection_addresses(monkeypatch):
    class FakeDefault:
        async def resolve(self, host, port, family):
            self.host = host
            return [{"host": self.result, "hostname": host, "port": port,
                     "family": family, "proto": 0, "flags": 0}]
        async def close(self):
            return None

    fake = FakeDefault()
    monkeypatch.setattr(images, "DefaultResolver", lambda: fake)
    for address in ["127.0.0.1", "169.254.169.254", "10.12.0.1", "::1", "fc00::1"]:
        fake.result = address
        with pytest.raises(OSError):
            await images._PublicResolver().resolve("apparently-public.example.com", 443)
    fake.result = "1.1.1.1"
    assert (await images._PublicResolver().resolve("public.example.com", 443))[0]["host"] == "1.1.1.1"


@pytest.mark.asyncio
async def test_preview_does_not_mutate_cookidoo_and_does_not_echo_signed_url():
    service, state = _fake_service()
    image = _prepared()
    url = "https://cdn.example.org/photo.png?token=do-not-return"
    async def download(_url):
        assert _url == url
        return image
    reply = json.loads(await images.preview(
        state.id, url, ensure_credentials=lambda: None,
        get_service=lambda: service, download_image=download,
    ))
    assert reply["action"] == "PREVIEW_ONLY"
    assert reply["will_replace_existing_picture"] is False
    assert reply["image_sha256"] == image.sha256
    assert reply["confirmation"].startswith(images.CONFIRM_PREFIX)
    assert "do-not-return" not in json.dumps(reply)
    assert reply["width"] == 120 and reply["height"] == 110


@pytest.mark.asyncio
async def test_preview_replacement_and_changed_source_or_photo_change_confirmation():
    service, state = _fake_service(old_image="previous.jpg")
    source = _prepared()
    async def download(_):
        return source
    first = json.loads(await images.preview(
        state.id, "https://cdn.example.com/image", ensure_credentials=lambda: None,
        get_service=lambda: service, download_image=download
    ))
    assert first["will_replace_existing_picture"]
    state.image = "some-new-manually-uploaded-photo.jpg"
    second = json.loads(await images.preview(
        state.id, "https://cdn.example.com/image", ensure_credentials=lambda: None,
        get_service=lambda: service, download_image=download
    ))
    assert first["confirmation"] != second["confirmation"]
    other = images._confirmation(state.id, _prepared(color=(2, 3, 4)), state.image)
    assert other != second["confirmation"]


@pytest.mark.asyncio
async def test_image_write_disabled_and_confirmation_enforced(monkeypatch):
    service, state = _fake_service()
    async def download(_):
        return _prepared()
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "false")
    with pytest.raises(ToolError, match="disabled"):
        await images.upload(state.id, "https://cdn.example.com/photo.png", "",
                            ensure_credentials=lambda: 1/0,
                            get_service=lambda: 1/0, download_image=download)
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    with pytest.raises(ToolError, match="changed since preview"):
        await images.upload(state.id, "https://cdn.example.com/photo.png", "wrong",
                            ensure_credentials=lambda: None,
                            get_service=lambda: service, download_image=download)


@pytest.mark.asyncio
async def test_existing_image_requires_explicit_replacement(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    service, state = _fake_service(old_image="already-existing.jpg")
    with pytest.raises(ToolError, match="already has a photo"):
        await images.upload(state.id, "https://cdn.example.com/photo.png", "x",
                            ensure_credentials=lambda: None,
                            get_service=lambda: service, download_image=lambda u: 1/0)


@pytest.mark.asyncio
async def test_success_reserves_once_then_verifies_image_readback(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_WRITE_GUARD_BUCKET", "aws2022-lambda-code")
    service, state = _fake_service()
    picture = _prepared()
    confirmation = images._confirmation(state.id, picture, "")
    async def download(_):
        return picture
    reservations = []
    monkeypatch.setattr(images, "_reserve_image", lambda *args: reservations.append(args))
    async def update(svc, rid, image):
        assert svc is service and rid == state.id and image is picture
        state.image = "https://img.cookidoo.co.uk/customer/photo-test.jpg"
        return "customer/photo-test.jpg"
    result = json.loads(await images.upload(
        state.id, "https://cdn.example.com/photo.png", confirmation,
        ensure_credentials=lambda: None, get_service=lambda: service,
        download_image=download, update_image=update
    ))
    assert result["updated"] and result["verified"]
    assert result["name"] == "Chicken Madras TM7"
    assert len(reservations) == 1
    assert reservations[0][0] == "aws2022-lambda-code"


@pytest.mark.asyncio
async def test_photo_changes_between_preview_and_upload_stops_without_reservation(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_WRITE_GUARD_BUCKET", "aws2022-lambda-code")
    service, state = _fake_service()
    confirmation = images._confirmation(state.id, _prepared(), "")
    state.image = "user-photo-uploaded-between-steps.jpg"
    monkeypatch.setattr(images, "_reserve_image", lambda *args: 1/0)
    async def download(_):
        return _prepared()
    with pytest.raises(ToolError, match="already has a photo"):
        await images.upload(state.id, "https://cdn.example.com/photo.png",
                            confirmation, ensure_credentials=lambda: None,
                            get_service=lambda: service, download_image=download)
    with pytest.raises(ToolError, match="changed since preview"):
        await images.upload(state.id, "https://cdn.example.com/photo.png",
                            confirmation, replace_existing=True,
                            ensure_credentials=lambda: None,
                            get_service=lambda: service, download_image=download)


@pytest.mark.asyncio
async def test_image_upload_failure_is_not_retried(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    monkeypatch.setenv("COOKIDOO_WRITE_GUARD_BUCKET", "aws2022-lambda-code")
    service, state = _fake_service()
    photo = _prepared()
    markers = []
    monkeypatch.setattr(images, "_reserve_image", lambda *args: markers.append(args))
    call = AsyncMock(side_effect=TimeoutError("private upstream response"))
    with pytest.raises(ToolError, match="uncertain outcome"):
        await images.upload(state.id, "https://cdn.example.com/photo.png",
                            images._confirmation(state.id, photo, ""),
                            ensure_credentials=lambda: None, get_service=lambda: service,
                            download_image=AsyncMock(return_value=photo), update_image=call)
    assert call.await_count == 1 and len(markers) == 1


def test_image_write_guard_conditional_and_private(monkeypatch):
    import boto3
    class FakeS3:
        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            assert kwargs["ServerSideEncryption"] == "AES256"
            assert kwargs["Key"].startswith("cookidoo-mcp/write-guards/images/")
            assert kwargs["Body"] == _prepared().sha256.encode("ascii")
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "internal"}},
                              "PutObject")
    monkeypatch.setattr(boto3, "client", lambda _: FakeS3())
    with pytest.raises(ToolError, match="already submitted"):
        images._reserve_image("aws2022-lambda-code", "recipe-test-id", _prepared())


@pytest.mark.asyncio
async def test_cloudinary_workflow_uses_only_signed_upload_and_image_only_patch(monkeypatch):
    events = []
    class FakeCookidoo:
        api_endpoint = __import__("yarl").URL("https://cookidoo.co.uk")
        localization = SimpleNamespace(language="en-GB")
        async def _request_json(self, method, url, operation, **kwargs):
            events.append((method, str(url), kwargs.get("json")))
            if method == "post":
                return {"signature": "test-only-cloudinary-signature"}
            assert method == "patch"
            return None
    api = FakeCookidoo()
    service = SimpleNamespace(client=AsyncMock(return_value=api))
    class Response:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return None
        async def json(self, **kw):
            return {"public_id": "customer-recipes/example-photo", "format": "jpg"}
    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs.get("trust_env") is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return None
        def post(self, url, **kwargs):
            assert url == images.CLOUDINARY_URL
            assert kwargs.get("allow_redirects") is False
            form = kwargs["data"]
            assert isinstance(form, __import__("aiohttp").FormData)
            return Response()
    monkeypatch.setattr(images, "ClientSession", FakeSession)
    ref = await images._cookidoo_image_update(
        service, "01M6P47BB8PG942TKR31TZ9FFE", images._normalize_image(_sample("JPEG"))
    )
    assert ref == "customer-recipes/example-photo.jpg"
    assert len(events) == 2
    assert events[0][0] == "post"
    assert events[0][1] == "https://cookidoo.co.uk/created-recipes/en-GB/image/signature"
    assert events[0][2]["upload_preset"] == images.CLOUDINARY_PRESET
    assert events[1][0] == "patch"
    assert events[1][1].endswith("/created-recipes/en-GB/01M6P47BB8PG942TKR31TZ9FFE")
    assert events[1][2] == {
        "image": "customer-recipes/example-photo.jpg", "isImageOwnedByUser": True
    }


@pytest.mark.asyncio
async def test_mcp_image_tools_visibility_and_approval_contract(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "false")
    async with Client(app.build_mcp()) as client:
        names = {x.name for x in await client.list_tools()}
    assert "preview_custom_recipe_image" in names
    assert "set_custom_recipe_image" not in names

    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    async with Client(app.build_mcp()) as client:
        tools = {x.name: x for x in await client.list_tools()}
    assert "set_custom_recipe_image" in tools
    schema = tools["set_custom_recipe_image"].inputSchema
    assert "confirmation" in schema["required"]
    assert "image_url" in schema["required"]
    assert "recipe_id" in schema["required"]
    assert "replace_existing" not in schema["required"]
