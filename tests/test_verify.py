"""Unit tests for telegram_reader.verify. Run: PYTHONPATH=src python -m unittest discover -s tests"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from mcp.server.fastmcp.exceptions import ToolError
from telethon import TelegramClient, errors
from telethon.tl import functions, types

import fakes
from telegram_reader import verify


def document_message(dc_id, payload=b"", media=None):
    doc = types.Document(
        id=1, access_hash=2, file_reference=b"ref", date=None, mime_type="video/mp4",
        size=len(payload), dc_id=dc_id, attributes=[],
    )
    # Telethon's Message.document also returns the file of a link preview.
    return SimpleNamespace(
        media=media or types.MessageMediaDocument(document=doc), document=doc, photo=None, payload=payload
    )


class RoutingClient:
    """Records where GetFileHashesRequest goes: the home connection or a borrowed one."""

    def __init__(self, home_dc, fail=None):
        self.session = SimpleNamespace(dc_id=home_dc)
        self.fail = fail
        self.home_requests = []
        self.dc_requests = []
        self.borrowed = []
        self.returned = []

    async def __call__(self, request):
        self.home_requests.append(request)
        return ["home"]

    async def _borrow_exported_sender(self, dc_id):
        self.borrowed.append(dc_id)
        return f"sender-{dc_id}"

    async def _call(self, sender, request):
        assert isinstance(request, functions.upload.GetFileHashesRequest)
        self.dc_requests.append(request)
        if self.fail:
            raise self.fail
        return [sender]

    async def _return_exported_sender(self, sender):
        self.returned.append(sender)


class PartHashesTest(unittest.IsolatedAsyncioTestCase):
    def test_telethon_still_has_the_private_methods_used_for_other_dcs(self):
        for name in ("_borrow_exported_sender", "_call", "_return_exported_sender"):
            self.assertTrue(callable(getattr(TelegramClient, name, None)), name)

    async def test_files_on_the_home_dc_use_the_main_connection(self):
        client = RoutingClient(home_dc=5)
        self.assertEqual(await verify.part_hashes(client, document_message(5), 4096), ["home"])
        (request,) = client.home_requests
        self.assertIsInstance(request.location, types.InputDocumentFileLocation)
        self.assertEqual((request.offset, client.borrowed), (4096, []))

    async def test_files_elsewhere_go_to_their_own_dc_and_return_the_sender(self):
        client = RoutingClient(home_dc=1)
        self.assertEqual(await verify.part_hashes(client, document_message(5), 1 << 20), ["sender-5"])
        self.assertEqual((client.home_requests, client.borrowed, client.returned), ([], [5], ["sender-5"]))
        self.assertEqual([r.offset for r in client.dc_requests], [1 << 20])
        failing = RoutingClient(home_dc=1, fail=errors.RPCError(None, "LOCATION_INVALID", 400))
        with self.assertRaises(errors.RPCError):
            await verify.part_hashes(failing, document_message(5), 0)
        self.assertEqual(failing.returned, ["sender-5"])

    async def test_a_link_preview_file_is_located_through_its_document(self):
        preview = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
        client = RoutingClient(home_dc=5)
        await verify.part_hashes(client, document_message(5, media=preview), 0)
        self.assertIsInstance(client.home_requests[0].location, types.InputDocumentFileLocation)


class FileMatchesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.offsets = []

    def open_with(self, data):
        path = os.path.join(self.tmp.name, "local")
        with open(path, "wb") as handle:
            handle.write(data)
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fd

    def fetcher(self, remote):
        msg = SimpleNamespace(payload=remote)

        async def fetch(offset):
            self.offsets.append(offset)
            return await fakes.part_hashes(msg, offset)

        return fetch

    async def test_identical_bytes_match_across_the_three_windows(self):
        data = os.urandom(3 * fakes.WINDOW + 1000)
        self.assertTrue(await verify.file_matches(self.fetcher(data), self.open_with(data), len(data)))
        self.assertEqual(self.offsets, [0, len(data) // 2, len(data) - 1])

    async def test_a_changed_byte_in_any_window_is_caught(self):
        data = bytearray(os.urandom(3 * fakes.WINDOW + 1000))
        for where in (0, len(data) // 2, len(data) - 1):
            with self.subTest(where=where):
                local = bytearray(data)
                local[where] ^= 1
                fetch = self.fetcher(bytes(data))
                self.assertFalse(await verify.file_matches(fetch, self.open_with(bytes(local)), len(data)))

    async def test_size_mismatches_and_empty_files_fail_without_asking_telegram(self):
        self.assertFalse(await verify.file_matches(self.fetcher(b"payload"), self.open_with(b"payload"), 8))
        self.assertFalse(await verify.file_matches(self.fetcher(b""), self.open_with(b""), None))
        self.assertEqual(self.offsets, [])

    async def test_a_small_file_needs_one_request(self):
        self.assertTrue(await verify.file_matches(self.fetcher(b"payload"), self.open_with(b"payload"), 7))
        self.assertEqual(self.offsets, [0])

    async def test_answers_that_miss_the_requested_offset_are_errors_not_mismatches(self):
        data = os.urandom(3 * fakes.WINDOW)
        wrong_window = self.fetcher(data)

        async def always_the_first_window(offset):
            return await wrong_window(0)

        async def nothing(offset):
            return []

        for fetch in (nothing, always_the_first_window):
            with self.subTest(fetch=fetch.__name__):
                with self.assertRaisesRegex(ToolError, "no part hashes"):
                    await verify.file_matches(fetch, self.open_with(data), len(data))


if __name__ == "__main__":
    unittest.main()
