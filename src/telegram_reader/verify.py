"""Check a local file against the SHA-256 part hashes Telegram keeps for a document.

upload.getFileHashes returns up to eight hashes of 128 KiB parts, covering the
1 MiB-aligned window around the requested offset. The last part of a file is
shorter than its stated limit, and its hash covers only the bytes that exist.
Sampling three windows (start, middle, end) costs at most three requests and
3 MiB of reading, which tells one upload from another of the same size without
reading it all. Damage between the windows goes unnoticed.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

from mcp.server.fastmcp.exceptions import ToolError
from telethon import functions, utils


async def part_hashes(client, msg, offset: int) -> list:
    """The FileHash parts Telegram reports around offset for msg's file."""
    # For a link preview msg.media is the web page; its file is what msg.document or msg.photo returns.
    dc_id, location = utils.get_input_location(msg.document or msg.photo)
    request = functions.upload.GetFileHashesRequest(location, offset)
    if dc_id is None or dc_id == client.session.dc_id:
        return await client(request)
    # The home DC answers LOCATION_INVALID for files stored elsewhere, so the
    # request goes to the file's own DC, the way Telethon's downloads do it.
    sender = await client._borrow_exported_sender(dc_id)
    try:
        return await client._call(sender, request)
    finally:
        await client._return_exported_sender(sender)


def _covers(part, offset: int) -> bool:
    return part.offset <= offset < part.offset + part.limit


def _read_parts(fd: int, parts: list) -> list[bytes]:
    # The thread owns this duplicate, so a cancelled caller closing its own
    # descriptor cannot leave the thread reading whatever file reuses the number.
    try:
        return [os.pread(fd, part.limit, part.offset) for part in parts]
    finally:
        os.close(fd)


async def file_matches(fetch_parts, fd: int, expected_size: int | None) -> bool:
    """Whether the open file fd holds the same bytes as the remote file, by sampled windows.

    fetch_parts(offset) returns the remote FileHash parts around offset.
    """
    size = os.fstat(fd).st_size
    if size == 0 or (expected_size is not None and size != expected_size):
        return False
    checked = []
    for offset in sorted({0, size // 2, size - 1}):
        # A small file fits in one window, which then answers for every offset.
        if any(_covers(part, offset) for part in checked):
            continue
        parts = await fetch_parts(offset)
        if not any(_covers(part, offset) for part in parts):
            raise ToolError("Telegram returned no part hashes for this part of the file; cannot compare it")
        chunks = await asyncio.to_thread(_read_parts, os.dup(fd), parts)
        if any(hashlib.sha256(chunk).digest() != part.hash for chunk, part in zip(chunks, parts)):
            return False
        checked.extend(parts)
    return True
