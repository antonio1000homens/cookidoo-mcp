"""Copying an existing Cookidoo picture is review-first and source-preserving."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image
import io

from src import app, recipe_image_copy as copying, recipe_images


SRC = "01M6P47BB8PG942TKR31TZ9FFE"
DST = "01M6P6PXZAZD18M84BW86VQ6XT"
PHOTO = "https://images.cookidoo.co.uk/user-content/chicken-madras.jpg"


def prepared():
    data = io.BytesIO()
    Image.new("RGB", (200, 180), (188, 27, 12)).save(data, "JPEG")
    return recipe_images._normalize_image(data.getvalue())


def service(source_photo=PHOTO, destination_photo=""):
    src = SimpleNamespace(id=SRC, name="Chicken Madras TM7", image=source_photo)
    dst = SimpleNamespace(
        id=DST, name="Chicken Madras TM7 – Browning Edition", image=destination_photo
    )
    own = SimpleNamespace(
        list_custom_recipes=AsyncMock(return_value=[src, dst]),
        get_custom_recipe=AsyncMock(side_effect=lambda key:
            (src if key == SRC else dst, [])),
    )
    return own, src, dst


@pytest.mark.asyncio
async def test_preview_two_owned_recipes_reads_image_without_writing():
    svc, src, dst = service()
    pic = prepared()
    download = AsyncMock(return_value=pic)
    preview = json.loads(await copying.preview(
        SRC, DST, ensure_credentials=lambda: None,
        get_service=lambda: svc, download_image=download,
    ))
    assert preview["source_recipe_name"] == src.name
    assert preview["destination_recipe_name"] == dst.name
    assert preview["will_replace_existing_picture"] is False
    assert preview["image_sha256"] == pic.sha256
    assert preview["confirmation"].startswith(copying.CONFIRM_PREFIX)
    assert "images.cookidoo.co.uk" not in json.dumps(preview)
    assert svc.get_custom_recipe.await_count == 2
    download.assert_awaited_once_with(PHOTO)


@pytest.mark.asyncio
async def test_missing_source_photo_is_rejected_and_does_not_download():
    svc, _, _ = service(source_photo="")
    with pytest.raises(ToolError, match="no picture"):
        await copying.preview(
            SRC, DST, ensure_credentials=lambda: None, get_service=lambda: svc,
            download_image=AsyncMock(side_effect=AssertionError("must not download"))
        )


@pytest.mark.asyncio
async def test_source_must_be_different_owned_recipe():
    svc, _, _ = service()
    with pytest.raises(ToolError, match="must differ"):
        await copying.preview(
            SRC, SRC, ensure_credentials=lambda: None, get_service=lambda: svc,
            download_image=AsyncMock(side_effect=AssertionError("must not download"))
        )


@pytest.mark.asyncio
async def test_preview_confirmation_changes_with_source_and_destination_state():
    svc, src, dst = service()
    pic = prepared()
    image_download = AsyncMock(return_value=pic)
    first = json.loads(await copying.preview(
        SRC, DST, ensure_credentials=lambda: None, get_service=lambda: svc,
        download_image=image_download,
    ))
    dst.image = "manually-uploaded-before-copy.jpg"
    after = json.loads(await copying.preview(
        SRC, DST, ensure_credentials=lambda: None, get_service=lambda: svc,
        download_image=image_download,
    ))
    assert after["will_replace_existing_picture"]
    assert first["confirmation"] != after["confirmation"]
    assert copying._token(DST, SRC, pic, "") != first["confirmation"]


@pytest.mark.asyncio
async def test_actual_copy_reuses_safe_signed_upload_path_only_after_confirmation(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    svc, src, dst = service()
    pic = prepared()
    upload = AsyncMock(return_value=json.dumps({
        "updated": True, "verified": True,
        "url": "https://cookidoo.co.uk/created-recipes/en-GB/" + DST,
    }))
    result = json.loads(await copying.copy(
        SRC, DST, copying._token(SRC, DST, pic, ""),
        ensure_credentials=lambda: None, get_service=lambda: svc,
        download_image=AsyncMock(return_value=pic), upload_image=upload,
    ))
    assert result["copied"] is True and result["source_unchanged"] is True
    assert result["destination_recipe_id"] == DST
    assert src.image == PHOTO  # no source mutation
    upload.assert_awaited_once()
    args = upload.await_args.args
    assert args[0] == DST and args[1] == PHOTO
    assert args[2] == recipe_images._confirmation(DST, pic, "")
    assert args[3] is False


@pytest.mark.asyncio
async def test_copy_refuses_stale_confirmation_and_existing_destination_image(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    svc, src, dst = service(destination_photo="already-on-destination.jpg")
    pic = prepared()
    upload = AsyncMock()
    with pytest.raises(ToolError, match="already has a photo"):
        await copying.copy(
            SRC, DST, copying._token(SRC, DST, pic, ""),
            ensure_credentials=lambda: None, get_service=lambda: svc,
            download_image=AsyncMock(return_value=pic), upload_image=upload,
        )
    with pytest.raises(ToolError, match="changed since preview"):
        await copying.copy(
            SRC, DST, copying._token(SRC, DST, pic, ""),
            replace_existing=True,
            ensure_credentials=lambda: None, get_service=lambda: svc,
            download_image=AsyncMock(return_value=pic), upload_image=upload,
        )
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_and_write_tool_visibility(monkeypatch):
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "false")
    async with Client(app.build_mcp()) as client:
        names = {t.name for t in await client.list_tools()}
    assert "preview_copy_custom_recipe_image" in names
    assert "copy_custom_recipe_image" not in names
    monkeypatch.setenv("COOKIDOO_RECIPE_CREATION_ENABLED", "true")
    async with Client(app.build_mcp()) as client:
        names = {t.name for t in await client.list_tools()}
    assert "preview_copy_custom_recipe_image" in names
    assert "copy_custom_recipe_image" in names
    assert len(names) == 12
