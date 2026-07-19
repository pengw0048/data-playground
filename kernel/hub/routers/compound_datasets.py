"""Read-only detail and checksum-bound range serving for the public compound fixture."""

from __future__ import annotations

import hashlib
import os
import stat

from fastapi import APIRouter, Request, Response

from hub.api_errors import APIError, APIErrorCode
from hub.compound_fixture import (
    FixtureUnavailable,
    fixture_asset_available,
    fixture_asset_path,
    fixture_authority,
    episode_reference_windows,
)
from hub.models import (
    CompoundFixtureAssetV1,
    CompoundFixtureDetailV1,
    CompoundFixtureEpisodeV1,
    CompoundFixtureStreamV1,
)


router = APIRouter()


def _authority(dataset_id: str, revision_id: str):
    try:
        authority = fixture_authority()
    except FixtureUnavailable as exc:
        raise APIError(
            410,
            "Compound fixture is unavailable",
            code=APIErrorCode.RESOURCE_GONE,
            retryable=False,
        ) from exc
    if (dataset_id, revision_id) != (
        authority.manifest.ref.dataset_id,
        authority.manifest.digest,
    ):
        raise APIError(
            404,
            "Compound revision not found",
            code=APIErrorCode.NOT_FOUND,
            retryable=False,
        )
    return authority


def _detail(authority) -> CompoundFixtureDetailV1:
    bindings = {
        (item.episode_id, item.stream_id): item for item in authority.manifest.bindings
    }
    windows = episode_reference_windows()
    return CompoundFixtureDetailV1(
        datasetId=authority.manifest.ref.dataset_id,
        revisionId=authority.manifest.digest,
        episodes=[
            CompoundFixtureEpisodeV1(
                id=episode.episode_id,
                referenceClockId="reference-ms",
                startTick=windows[episode.episode_id][0],
                endTick=windows[episode.episode_id][1],
                streams=[
                    CompoundFixtureStreamV1(
                        id=stream.id,
                        kind=stream.kind,
                        clockId=stream.clock.id,
                        state=bindings[(episode.episode_id, stream.id)].state,
                        assetIds=list(
                            bindings[(episode.episode_id, stream.id)].asset_ids
                        ),
                    )
                    for stream in authority.manifest.streams
                ],
            )
            for episode in authority.manifest.episodes
        ],
        assets=[
            CompoundFixtureAssetV1(
                id=asset.id,
                mediaType=asset.media_type,
                byteLength=asset.byte_length,
                sha256=asset.sha256,
                status=("available" if fixture_asset_available(authority) else "unavailable"),
            )
            for asset in authority.manifest.assets
        ],
    )


def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError
    start_text, separator, end_text = value[6:].partition("-")
    if not separator or (not start_text and not end_text):
        raise ValueError
    if not start_text:
        if not end_text.isdecimal():
            raise ValueError
        length = int(end_text)
        if length <= 0:
            raise ValueError
        return max(0, size - length), size - 1
    if not start_text.isdecimal() or (end_text and not end_text.isdecimal()):
        raise ValueError
    start = int(start_text)
    if start >= size:
        raise ValueError
    end = size - 1 if not end_text else min(int(end_text), size - 1)
    if end < start:
        raise ValueError
    return start, end


def _opened_verified_asset(path, asset) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    handle = None
    try:
        handle = os.fdopen(os.open(path, flags), "rb", closefd=True)
        facts = os.fstat(handle.fileno())
        if not stat.S_ISREG(facts.st_mode) or facts.st_size != asset.byte_length:
            raise FixtureUnavailable(
                "fixture asset declaration no longer matches bytes"
            )
        snapshot = handle.read(asset.byte_length + 1)
        if (len(snapshot) != asset.byte_length
                or hashlib.sha256(snapshot).hexdigest() != asset.sha256):
            raise FixtureUnavailable("fixture asset changed during snapshot")
        return snapshot
    finally:
        if handle is not None:
            handle.close()


_ASSET_BASE_HEADERS = {
    "Accept-Ranges": {"schema": {"type": "string"}},
    "Content-Length": {"schema": {"type": "string"}},
    "ETag": {"schema": {"type": "string"}},
}
_ASSET_RANGE_HEADERS = {**_ASSET_BASE_HEADERS, "Content-Range": {"schema": {"type": "string"}}}
_ASSET_GET_RESPONSES = {
    200: {"description": "Full immutable asset", "headers": _ASSET_BASE_HEADERS,
          "content": {"video/webm": {"schema": {"type": "string", "format": "binary"}}}},
    206: {"description": "One satisfiable byte range", "headers": _ASSET_RANGE_HEADERS,
          "content": {"video/webm": {"schema": {"type": "string", "format": "binary"}}}},
    416: {"description": "Unsatisfiable or multi-range request", "headers": {
        "Content-Range": {"schema": {"type": "string"}},
    }},
}
_ASSET_HEAD_RESPONSES = {
    200: {"description": "Full immutable asset", "headers": _ASSET_BASE_HEADERS},
    206: {"description": "One satisfiable byte range", "headers": _ASSET_RANGE_HEADERS},
    416: _ASSET_GET_RESPONSES[416],
}


def _asset_for_request(authority, episode_id: str, stream_id: str, asset_id: str):
    try:
        asset = next(item for item in authority.manifest.assets if item.id == asset_id)
        return asset, fixture_asset_path(
            authority, episode_id=episode_id, stream_id=stream_id, asset_id=asset_id)
    except (FixtureUnavailable, StopIteration) as exc:
        raise APIError(
            410, "Compound fixture asset is unavailable",
            code=APIErrorCode.RESOURCE_GONE, retryable=False,
        ) from exc


def _snapshot_or_error(path, asset) -> bytes:
    try:
        return _opened_verified_asset(path, asset)
    except PermissionError as exc:
        raise APIError(403, "Compound fixture asset is not readable", code=APIErrorCode.PERMISSION_DENIED,
                       retryable=False) from exc
    except (FixtureUnavailable, OSError):
        raise


def _range_response(
    request: Request,
    dataset_id: str,
    revision_id: str,
    episode_id: str,
    stream_id: str,
    asset_id: str,
    *,
    head_only: bool,
) -> Response:
    authority = _authority(dataset_id, revision_id)
    try:
        asset, path = _asset_for_request(authority, episode_id, stream_id, asset_id)
        snapshot = _snapshot_or_error(path, asset)
    except APIError:
        raise
    except (FixtureUnavailable, OSError) as exc:
        raise APIError(
            410,
            "Compound fixture asset is unavailable",
            code=APIErrorCode.RESOURCE_GONE,
            retryable=False,
        ) from exc
    try:
        requested = _parse_range(request.headers.get("range"), asset.byte_length)
    except ValueError:
        return Response(
            status_code=416, headers={"Content-Range": f"bytes */{asset.byte_length}"}
        )
    start, end = requested if requested is not None else (0, asset.byte_length - 1)
    length, status = end - start + 1, 206 if requested is not None else 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "ETag": f'"{asset.sha256}"',
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{asset.byte_length}"
    if head_only:
        return Response(
            status_code=status, media_type=asset.media_type, headers=headers
        )
    return Response(snapshot[start:end + 1], status_code=status, media_type=asset.media_type, headers=headers)


@router.get(
    "/compound-datasets/{dataset_id}/revisions/{revision_id}",
    response_model=CompoundFixtureDetailV1,
)
def compound_detail(dataset_id: str, revision_id: str) -> CompoundFixtureDetailV1:
    """Return fixture metadata only; manifest bytes and all physical authority stay private."""
    return _detail(_authority(dataset_id, revision_id))


@router.get(
    "/compound-datasets/reference",
    response_model=CompoundFixtureDetailV1,
)
def compound_reference() -> CompoundFixtureDetailV1:
    """Discover the one public opaque fixture without a prior private identifier."""
    try:
        return _detail(fixture_authority())
    except FixtureUnavailable as exc:
        raise APIError(410, "Compound fixture is unavailable", code=APIErrorCode.RESOURCE_GONE,
                       retryable=False) from exc


@router.get(
    "/compound-datasets/{dataset_id}/revisions/{revision_id}/episodes/{episode_id}/streams/{stream_id}/assets/{asset_id}",
    response_class=Response,
    responses=_ASSET_GET_RESPONSES,
)
def compound_asset_get(
    request: Request,
    dataset_id: str,
    revision_id: str,
    episode_id: str,
    stream_id: str,
    asset_id: str,
) -> Response:
    return _range_response(
        request,
        dataset_id,
        revision_id,
        episode_id,
        stream_id,
        asset_id,
        head_only=False,
    )


@router.head(
    "/compound-datasets/{dataset_id}/revisions/{revision_id}/episodes/{episode_id}/streams/{stream_id}/assets/{asset_id}",
    response_class=Response,
    responses=_ASSET_HEAD_RESPONSES,
)
def compound_asset_head(
    request: Request,
    dataset_id: str,
    revision_id: str,
    episode_id: str,
    stream_id: str,
    asset_id: str,
) -> Response:
    return _range_response(
        request,
        dataset_id,
        revision_id,
        episode_id,
        stream_id,
        asset_id,
        head_only=True,
    )
