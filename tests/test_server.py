"""Unit tests for telegram_reader.server. Run: PYTHONPATH=src python -m unittest discover -s tests"""
import asyncio
import contextlib
import gc
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from mcp.server.fastmcp.exceptions import ToolError
from telethon import errors, utils
from telethon.tl import types

from telegram_reader import downloads as dl
import fakes
from telegram_reader import server as tr

UTC = timezone.utc
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

PUBLIC_CHANNEL = fakes.channel(1001, "News", username="newsfeed", broadcast=True)
PRIVATE_CHANNEL = fakes.channel(1002, "Private Feed", broadcast=True)
SUPERGROUP = fakes.channel(1003, "Chat Room", username="chatroom", megagroup=True)
BASIC_GROUP = types.Chat(
    id=2001, title="Family", photo=types.ChatPhotoEmpty(), participants_count=3, date=None, version=1
)
FRIEND = types.User(id=3001, first_name="Alice", username="alice")
BOT = types.User(id=3002, first_name="Helper", username="helper_bot", bot=True)
SAVED = types.User(id=3003, first_name="Owner", is_self=True)
INBOX = types.Channel(
    id=1004, title="News DMs", photo=types.ChatPhotoEmpty(), date=None, monoforum=True
)
GONE = types.ChannelForbidden(id=1005, access_hash=0, title="Gone")


def marked(entity):
    return utils.get_peer_id(entity)


class ChatKindTest(unittest.TestCase):
    def test_broadcast_channel_is_channel(self):
        self.assertEqual(tr.chat_kind(PUBLIC_CHANNEL), "channel")

    def test_supergroup_and_basic_group_are_groups(self):
        self.assertEqual(tr.chat_kind(SUPERGROUP), "group")
        self.assertEqual(tr.chat_kind(BASIC_GROUP), "group")

    def test_private_and_unknown_entities_are_refused(self):
        for entity in (FRIEND, BOT, SAVED, INBOX, GONE, types.ChatForbidden(id=9, title="x"), None):
            with self.subTest(entity=type(entity).__name__):
                self.assertIsNone(tr.chat_kind(entity))


class ParseMessageLinkTest(unittest.TestCase):
    def test_public_link(self):
        self.assertEqual(tr.parse_message_link("https://t.me/newsfeed/123"), ("newsfeed", 123))

    def test_preview_and_comment_forms(self):
        self.assertEqual(tr.parse_message_link("t.me/s/newsfeed/12"), ("newsfeed", 12))
        self.assertEqual(tr.parse_message_link("https://t.me/newsfeed/7?comment=4"), ("newsfeed", 7))

    def test_private_link_maps_to_the_marked_channel_id(self):
        # Short ids must not be glued onto "-100": channel 1001 is -1000000001001.
        self.assertEqual(tr.parse_message_link("https://t.me/c/1001/89"), (marked(PUBLIC_CHANNEL), 89))
        self.assertEqual(tr.parse_message_link("https://t.me/c/1234567890/1"), (-1001234567890, 1))

    def test_topic_links_take_the_last_number(self):
        self.assertEqual(tr.parse_message_link("https://t.me/c/1001/5/89"), (marked(PUBLIC_CHANNEL), 89))
        self.assertEqual(tr.parse_message_link("https://t.me/chatroom/5/89"), ("chatroom", 89))

    def test_rejects_non_message_links(self):
        for bad in (
            "https://example.com/foo/1",
            "https://t.me/newsfeed",
            "https://t.me/c/abc/1",
            "https://t.me/newsfeed/0",
            "https://t.me/+8613800000000/1",
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tr.parse_message_link(bad)


class ParseChatRefTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(tr.parse_chat_ref("-1001234"), -1001234)
        self.assertEqual(tr.parse_chat_ref(-1001234), -1001234)
        self.assertEqual(tr.parse_chat_ref("@newsfeed"), "newsfeed")
        self.assertEqual(tr.parse_chat_ref("https://t.me/newsfeed"), "newsfeed")
        self.assertEqual(tr.parse_chat_ref("https://t.me/newsfeed/123"), "newsfeed")
        self.assertEqual(tr.parse_chat_ref(" newsfeed "), "newsfeed")

    def test_private_channel_links_use_the_marked_channel_id(self):
        for link in (
            "https://t.me/c/1002",
            "t.me/c/1002/89",
            "https://t.me/c/1002/5/89?single",
        ):
            with self.subTest(link=link):
                self.assertEqual(tr.parse_chat_ref(link), marked(PRIVATE_CHANNEL))

    def test_phone_numbers_and_special_names_never_reach_telethon(self):
        for bad in (
            "+8613800000000", "me", "  ", "news feed", "https://t.me/+AbCdEf",
            "https://t.me/c/abc/1", "https://t.me/c/0/1", "https://t.me/c/-1/1",
            "https://t.me/c", "https://t.me/c/+1002/1",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tr.parse_chat_ref(bad)


class ParseProxyTest(unittest.TestCase):
    def test_http_proxy(self):
        self.assertEqual(
            tr.parse_proxy("http://192.0.2.10:8080"),
            {"proxy_type": "http", "addr": "192.0.2.10", "port": 8080},
        )

    def test_unset_means_direct(self):
        self.assertIsNone(tr.parse_proxy(""))
        self.assertIsNone(tr.parse_proxy(None))

    def test_bad_proxy_error_does_not_echo_the_url(self):
        with self.assertRaises(ValueError) as caught:
            tr.parse_proxy("ftp://user:secret@192.0.2.10")
        self.assertNotIn("secret", str(caught.exception))

    def test_proxy_credentials_are_refused_without_echoing_them(self):
        for url in (
            "http://user:secret@192.0.2.10:8080",
            "socks5://user@127.0.0.1:1080",
            "socks4://:secret@127.0.0.1:1080",
            "http://@127.0.0.1:8080",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    tr.parse_proxy(url)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn(url, str(caught.exception))


class ChatDirNameTest(unittest.TestCase):
    def test_names_always_carry_the_numeric_id(self):
        self.assertEqual(tr.chat_dir_name(PUBLIC_CHANNEL), "newsfeed-c1001")
        self.assertEqual(tr.chat_dir_name(PRIVATE_CHANNEL), "c1002")
        self.assertEqual(tr.chat_dir_name(BASIC_GROUP), "g2001")

    def test_a_username_cannot_take_another_chats_directory(self):
        squatter = fakes.channel(5555, "Squatter", username="c1002", broadcast=True)
        self.assertNotEqual(tr.chat_dir_name(squatter), tr.chat_dir_name(PRIVATE_CHANNEL))


class MessageLinkTest(unittest.TestCase):
    def test_links(self):
        self.assertEqual(tr.message_link(PUBLIC_CHANNEL, 5), "https://t.me/newsfeed/5")
        self.assertEqual(tr.message_link(PRIVATE_CHANNEL, 5), "https://t.me/c/1002/5")
        self.assertIsNone(tr.message_link(BASIC_GROUP, 5))

    def test_private_link_round_trips_through_the_parser(self):
        link = tr.message_link(PRIVATE_CHANNEL, 5)
        self.assertEqual(tr.parse_message_link(link), (marked(PRIVATE_CHANNEL), 5))


class SerializeMessageTest(unittest.TestCase):
    def test_channel_post_with_hidden_links_buttons_and_file(self):
        msg = fakes.message(
            42,
            NOW,
            "Read more here",
            PUBLIC_CHANNEL,
            entities=[types.MessageEntityTextUrl(offset=10, length=4, url="https://example.com/a")],
            reply_markup=types.ReplyInlineMarkup(
                rows=[
                    types.KeyboardButtonRow(
                        buttons=[
                            types.KeyboardButton(
                                text="Open", type=types.InlineButtonTypeUrl(url="https://example.com/b")
                            )
                        ]
                    )
                ]
            ),
            document=object(),
            file=SimpleNamespace(name="a.zip", mime_type="application/zip", size=2048),
            views=900,
            grouped_id=77,
        )
        out = tr.serialize_message(msg, PUBLIC_CHANNEL)
        self.assertEqual(out["id"], 42)
        self.assertEqual(out["date"], "2026-10-14T12:00:00+00:00")
        self.assertEqual(out["text"], "Read more here")
        self.assertEqual(out["link"], "https://t.me/newsfeed/42")
        self.assertEqual(out["hidden_links"], ["https://example.com/a"])
        self.assertEqual(out["buttons"], [{"text": "Open", "url": "https://example.com/b"}])
        self.assertEqual(
            out["media"],
            {"type": "document", "name": "a.zip", "mime_type": "application/zip", "size": 2048},
        )
        self.assertEqual(out["views"], 900)
        self.assertEqual(out["album_id"], 77)
        self.assertNotIn("sender", out)

    def test_group_message_names_sender_and_forward_source(self):
        msg = fakes.message(
            7,
            NOW,
            "hi",
            SUPERGROUP,
            sender=FRIEND,
            forward=SimpleNamespace(chat=PUBLIC_CHANNEL, sender=None, from_name=None),
            reply_to_msg_id=6,
        )
        out = tr.serialize_message(msg, SUPERGROUP)
        self.assertEqual(out["sender"], {"name": "Alice", "username": "alice"})
        self.assertEqual(out["forwarded_from"], "News")
        self.assertEqual(out["reply_to"], 6)

    def test_contact_cards_report_only_their_presence(self):
        msg = fakes.message(8, NOW, "", SUPERGROUP, contact=SimpleNamespace(phone_number="+8613800000000"))
        self.assertEqual(tr.serialize_message(msg, SUPERGROUP)["media"], {"type": "contact"})


def make_client(**overrides):
    posts = [fakes.message(i, NOW - timedelta(hours=10 - i), f"post {i}", PUBLIC_CHANNEL) for i in range(1, 11)]
    def clip(msg_id, text, grouped_id, chat=SUPERGROUP, **fields):
        return fakes.message(msg_id, NOW, text, chat, grouped_id=grouped_id, video=object(), payload=b"v",
                             file=SimpleNamespace(name=f"VID_{msg_id}.mp4", ext=".mp4", mime_type="video/mp4", size=1),
                             **fields)

    # Album 900 is 21, 22 and 24, captioned on 21. 23 belongs to another album, and
    # 25 is a private-chat message in the same id range; both must be left out.
    album = [clip(21, "合集 第1期\n#tag", 900), clip(22, "", 900), clip(23, "", 901), clip(24, "", 900),
             clip(25, "", 900, chat=FRIEND)]
    media = album + [
        fakes.message(11, NOW, "", SUPERGROUP, photo=object(), payload=b"\xff\xd8jpeg",
                      file=SimpleNamespace(name=None, ext=".jpg", mime_type="image/jpeg", size=6)),
        fakes.message(12, NOW, "", SUPERGROUP, photo=object(), payload=b"big",
                      file=SimpleNamespace(name=None, ext=".jpg", mime_type="image/jpeg",
                                           size=tr.MAX_INLINE_PHOTO_BYTES + 1)),
        fakes.message(13, NOW, "", SUPERGROUP, document=object(), payload=b"zipdata",
                      file=SimpleNamespace(name="pack.zip", ext=".zip", mime_type="application/zip", size=7)),
    ]
    family = [fakes.message(i, NOW, f"family {i}", BASIC_GROUP) for i in (1, 2)]
    settings = dict(
        entities={
            "newsfeed": PUBLIC_CHANNEL,
            marked(PUBLIC_CHANNEL): PUBLIC_CHANNEL,
            "chatroom": SUPERGROUP,
            marked(SUPERGROUP): SUPERGROUP,
            marked(BASIC_GROUP): BASIC_GROUP,
            "alice": FRIEND,
            "helper_bot": BOT,
            marked(INBOX): INBOX,
            marked(GONE): GONE,
        },
        history={PUBLIC_CHANNEL.id: posts, SUPERGROUP.id: media, BASIC_GROUP.id: family},
        dialogs=[
            fakes.dialog(PUBLIC_CHANNEL, marked(PUBLIC_CHANNEL), unread=3),
            fakes.dialog(FRIEND, marked(FRIEND), unread=1),
            fakes.dialog(SUPERGROUP, marked(SUPERGROUP)),
            fakes.dialog(INBOX, marked(INBOX), unread=2),
            fakes.dialog(SAVED, marked(SAVED)),
            fakes.dialog(BASIC_GROUP, marked(BASIC_GROUP)),
        ],
        global_results=[
            # Every hit matches the query, so only the privacy filter can drop one.
            fakes.message(1, NOW, "dm hit", FRIEND),
            fakes.message(2, NOW, "channel hit", PUBLIC_CHANNEL),
            fakes.message(3, NOW, "bot hit", BOT),
            fakes.message(4, NOW, "saved hit", SAVED),
            fakes.message(5, NOW, "inbox hit", INBOX),
            # Telegram left the chat out of the response, so Telethon has no entity for it.
            fakes.message(6, NOW, "orphan hit", None, peer_id=types.PeerChannel(1001)),
            fakes.message(7, NOW, "group hit", SUPERGROUP),
        ],
    )
    settings.update(overrides)
    return fakes.FakeClient(**settings)


class ReaderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = make_client()
        self.reader = tr.Reader(self.client)

    async def test_read_messages_is_newest_first_and_paginates(self):
        out = await self.reader.read_messages("@newsfeed", limit=3)
        self.assertEqual(out["chat"]["kind"], "channel")
        self.assertEqual([m["id"] for m in out["messages"]], [10, 9, 8])
        self.assertEqual(out["next_before_id"], 8)
        out = await self.reader.read_messages("newsfeed", limit=3, before_id=8)
        self.assertEqual([m["id"] for m in out["messages"]], [7, 6, 5])

    async def test_read_messages_stops_at_since(self):
        since = (NOW - timedelta(hours=2, minutes=30)).isoformat()
        out = await self.reader.read_messages("newsfeed", limit=50, since=since)
        self.assertEqual([m["id"] for m in out["messages"]], [10, 9, 8])
        self.assertIsNone(out["next_before_id"])

    async def test_read_messages_clamps_limit(self):
        await self.reader.read_messages("newsfeed", limit=10_000)
        self.assertEqual(self.client.iter_calls[-1]["limit"], tr.MAX_LIMIT)

    async def test_list_chats_ids_feed_read_messages(self):
        listed = await self.reader.list_chats()
        self.assertEqual([c["title"] for c in listed["chats"]], ["News", "Chat Room", "Family"])
        self.assertEqual(listed["chats"][0]["unread_count"], 3)
        out = await self.reader.read_messages(listed["chats"][2]["id"])
        self.assertEqual([m["text"] for m in out["messages"]], ["family 2", "family 1"])

    async def test_list_chats_filters_by_kind_and_query(self):
        out = await self.reader.list_chats(kind="group", query="ROOM")
        self.assertEqual([c["title"] for c in out["chats"]], ["Chat Room"])

    async def test_private_chats_are_refused_everywhere(self):
        for chat in ("@alice", "helper_bot", marked(INBOX), "https://t.me/c/1004/1"):
            with self.subTest(chat=chat):
                with self.assertRaisesRegex(ToolError, "channels and groups"):
                    await self.reader.read_messages(chat)
                with self.assertRaisesRegex(ToolError, "channels and groups"):
                    await self.reader.search_messages("x", chat=chat)
                with self.assertRaisesRegex(ToolError, "channels and groups"):
                    await self.reader.get_message(chat, message_id=1)
        with self.assertRaisesRegex(ToolError, "channels and groups"):
            await self.reader.get_message("https://t.me/alice/1")
        self.assertEqual(self.client.iter_calls, [])
        self.assertEqual(self.client.get_calls, [])

    async def test_forbidden_chats_say_no_access(self):
        with self.assertRaisesRegex(ToolError, "no access"):
            await self.reader.read_messages(marked(GONE))

    async def test_unknown_chat_is_a_tool_error(self):
        with self.assertRaisesRegex(ToolError, "list_chats"):
            await self.reader.read_messages("nobody")

    async def test_global_search_returns_only_known_channels_and_groups(self):
        out = await self.reader.search_messages("hit", limit=10)
        self.assertEqual([m["text"] for m in out["messages"]], ["channel hit", "group hit"])
        self.assertEqual([m["chat"]["title"] for m in out["messages"]], ["News", "Chat Room"])
        self.assertFalse(out["truncated"])

    async def test_search_drops_mismatched_peers_in_both_scopes(self):
        stray = fakes.message(1, NOW, "private hit", PUBLIC_CHANNEL, peer_id=types.PeerUser(FRIEND.id))
        allowed = fakes.message(2, NOW, "channel hit", PUBLIC_CHANNEL)
        for chat in ("newsfeed", ""):
            with self.subTest(chat=chat):
                client = make_client(
                    history={PUBLIC_CHANNEL.id: [stray, allowed]},
                    global_results=[stray, allowed],
                )
                result = await tr.Reader(client).search_messages("hit", chat=chat)
                self.assertEqual([msg["id"] for msg in result["messages"]], [allowed.id])

    async def test_global_search_reports_when_private_hits_exhaust_the_scan_budget(self):
        private = [fakes.message(i, NOW, "hit", FRIEND) for i in (1, 2, 3)]
        public = fakes.message(4, NOW, "hit", PUBLIC_CHANNEL)
        for hits, expected_ids, truncated in (
            (private + [public], [], True),
            (private[:2], [], False),
            (private[:2] + [public], [public.id], False),
        ):
            with self.subTest(expected_ids=expected_ids, truncated=truncated):
                client = make_client(global_results=hits)
                out = await tr.Reader(client).search_messages("hit", limit=1)
                self.assertEqual([m["id"] for m in out["messages"]], expected_ids)
                self.assertEqual(out["truncated"], truncated)
                self.assertEqual(client.iter_calls[-1]["limit"], 3)

    async def test_private_channel_links_feed_history_and_search(self):
        message = fakes.message(89, NOW, "channel hit", PRIVATE_CHANNEL)
        client = make_client(
            entities={marked(PRIVATE_CHANNEL): PRIVATE_CHANNEL},
            history={PRIVATE_CHANNEL.id: [message]},
        )
        reader = tr.Reader(client)
        link = "https://t.me/c/1002/89"
        for out in (
            await reader.read_messages(link),
            await reader.search_messages("hit", chat=link),
        ):
            self.assertEqual(out["chat"]["id"], marked(PRIVATE_CHANNEL))
            self.assertEqual([m["id"] for m in out["messages"]], [89])

    async def test_id_shared_with_a_private_chat_is_not_returned(self):
        stray = fakes.message(9, NOW, "dm text", FRIEND)
        reader = tr.Reader(make_client(by_id={9: stray}))
        with self.assertRaisesRegex(ToolError, "not found"):
            await reader.get_message(marked(BASIC_GROUP), message_id=9)

    async def test_history_drops_messages_from_another_peer_but_keeps_paging(self):
        posts = [fakes.message(i, NOW, f"post {i}", PUBLIC_CHANNEL) for i in (1, 2, 3)]
        posts[1].peer_id = types.PeerUser(FRIEND.id)
        reader = tr.Reader(make_client(history={PUBLIC_CHANNEL.id: posts}))
        out = await reader.read_messages("newsfeed", limit=3)
        self.assertEqual([m["id"] for m in out["messages"]], [3, 1])
        self.assertEqual(out["next_before_id"], 1)

    async def test_get_message_by_link_and_by_id(self):
        out, photo = await self.reader.get_message("https://t.me/newsfeed/4")
        self.assertEqual(out["message"]["text"], "post 4")
        self.assertIsNone(photo)
        out, _ = await self.reader.get_message(str(marked(PUBLIC_CHANNEL)), message_id=5)
        self.assertEqual(out["message"]["text"], "post 5")
        with self.assertRaisesRegex(ToolError, "not found"):
            await self.reader.get_message("https://t.me/newsfeed/999")
        with self.assertRaisesRegex(ToolError, "positive"):
            await self.reader.get_message("newsfeed", message_id=-1)

    async def test_every_content_result_carries_the_owner_rule(self):
        results = [
            await self.reader.list_chats(),
            await self.reader.read_messages("newsfeed", limit=1),
            await self.reader.search_messages("hit"),
            (await self.reader.get_message("https://t.me/newsfeed/4"))[0],
        ]
        for result in results:
            self.assertEqual(result["notice"], tr.NOTICE)

    async def test_oversized_photos_are_not_inlined(self):
        out, photo = await self.reader.get_message("https://t.me/chatroom/12")
        self.assertIsNone(photo)
        self.assertIn("download_media", out["photo_note"])
        self.assertEqual(self.client.download_calls, [])

    async def test_download_media_lands_in_the_chat_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "telegram"
            root.mkdir()
            reader = tr.Reader(self.client, dl.Downloads(self.client, str(root)))
            started = await reader.download_media("https://t.me/chatroom/13", name="Pack Vol.1")
            self.assertEqual(started["notice"], tr.NOTICE)
            (job,) = started["jobs"]
            done = await reader._downloads.wait(job["job_id"])
            self.assertEqual(done["status"], "done")
            self.assertEqual((root / "chatroom-c1003" / "Pack Vol.1 [13].zip").read_bytes(), b"zipdata")
            status = reader.download_status(job["job_id"])
            self.assertEqual((status["status"], status["notice"]), ("done", tr.NOTICE))

    async def test_a_name_with_a_replacement_character_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = tr.Reader(self.client, dl.Downloads(self.client, tmp))
            with self.assertRaisesRegex(ToolError, "U\\+FFFD"):
                await reader.download_media("https://t.me/chatroom/13", name="Pack �Vol.1")
            self.assertEqual(self.client.download_calls, [])
            self.assertEqual(reader.download_status()["jobs"], [])

    async def test_an_album_downloads_whole_and_takes_its_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "telegram"
            root.mkdir()
            reader = tr.Reader(self.client, dl.Downloads(self.client, str(root)))
            started = await reader.download_media("https://t.me/chatroom/22")
            names = []
            for job in started["jobs"]:
                names.append(Path((await reader._downloads.wait(job["job_id"]))["path"]).name)
            self.assertEqual(names, ["合集 第1期 - 01 [21].mp4", "合集 第1期 - 02 [22].mp4", "合集 第1期 - 03 [24].mp4"])

    async def test_an_album_that_does_not_fit_is_refused_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = dl.Downloads(self.client, tmp)
            downloads.free_slots = lambda: 2
            reader = tr.Reader(self.client, downloads)
            with self.assertRaisesRegex(ToolError, "too many downloads"):
                await reader.download_media("https://t.me/chatroom/21")
            self.assertEqual(self.client.download_calls, [])

    @contextlib.asynccontextmanager
    async def gated_download_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = asyncio.Event()
            download = self.client.download_media

            async def gated(*args, **kwargs):
                await gate.wait()
                return await download(*args, **kwargs)

            downloads = dl.Downloads(self.client, tmp)
            with mock.patch.object(self.client, "download_media", side_effect=gated):
                try:
                    yield tr.Reader(self.client, downloads), gate
                finally:
                    tasks = [job.task for job in downloads._jobs.values() if job.task is not None]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def queue_posts(self, reader, count):
        for msg_id in range(100, 100 + count):
            self.client.by_id[msg_id] = fakes.message(
                msg_id, NOW, "", SUPERGROUP, document=object(), payload=b"queued",
                file=SimpleNamespace(name=f"file{msg_id}.zip", ext=".zip", size=6),
            )
            await reader.download_media("chatroom", message_id=msg_id)

    async def test_full_queue_reuses_active_jobs_and_refuses_new_posts(self):
        async with self.gated_download_reader() as (reader, _):
            await self.queue_posts(reader, 20)
            job_id = f"{marked(SUPERGROUP)}:100"
            task = reader._downloads._jobs[job_id].task
            retried = await reader.download_media("chatroom", message_id=100)
            self.assertEqual(retried["jobs"][0]["job_id"], job_id)
            self.assertIs(reader._downloads._jobs[job_id].task, task)
            with self.assertRaisesRegex(ToolError, "too many downloads"):
                await reader.download_media("chatroom", message_id=13)
            self.assertEqual(len(reader._downloads._jobs), 20)

    async def test_full_queue_reuses_only_unchanged_completed_jobs(self):
        async with self.gated_download_reader() as (reader, gate):
            first = await reader.download_media("chatroom", message_id=13)
            job_id = first["jobs"][0]["job_id"]
            gate.set()
            done = await reader._downloads.wait(job_id)
            self.assertEqual(done["status"], "done")
            gate.clear()
            await self.queue_posts(reader, 20)
            retried = await reader.download_media("chatroom", message_id=13)
            self.assertEqual(retried["jobs"][0]["status"], "done")
            self.assertEqual(self.client.download_calls, [13])
            Path(done["path"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ToolError, "too many downloads"):
                await reader.download_media("chatroom", message_id=13)
            self.assertEqual(reader.download_status(job_id)["status"], "failed")
            self.assertEqual(len(reader._downloads._jobs), 21)

    async def test_mixed_album_counts_only_new_jobs_against_capacity(self):
        async with self.gated_download_reader() as (reader, _):
            for number, msg_id in enumerate((21, 22), 1):
                message = await self.client.get_messages(SUPERGROUP, ids=msg_id)
                await reader._downloads.start(
                    f"{marked(SUPERGROUP)}:{msg_id}", tr.chat_dir_name(SUPERGROUP), "Chat Room",
                    message, "合集 第1期", f"{number:02d}",
                )
            await self.queue_posts(reader, 17)
            result = await reader.download_media("https://t.me/chatroom/21")
            self.assertEqual([job["message_id"] for job in result["jobs"]], [21, 22, 24])
            self.assertTrue(all(job["status"] in ("queued", "running") for job in result["jobs"]))
            self.assertEqual(len(reader._downloads._jobs), 20)
            self.assertEqual(reader._downloads.free_slots(), 0)

    async def test_mixed_album_is_refused_whole_when_new_jobs_do_not_fit(self):
        async with self.gated_download_reader() as (reader, _):
            message = await self.client.get_messages(SUPERGROUP, ids=21)
            await reader._downloads.start(
                f"{marked(SUPERGROUP)}:21", tr.chat_dir_name(SUPERGROUP), "Chat Room", message,
            )
            await self.queue_posts(reader, 18)
            existing = set(reader._downloads._jobs)
            with self.assertRaisesRegex(ToolError, "too many downloads"):
                await reader.download_media("https://t.me/chatroom/21")
            self.assertEqual(set(reader._downloads._jobs), existing)

    async def test_album_reuses_completed_members_when_new_job_evicts_their_records(self):
        async with self.gated_download_reader() as (reader, gate):
            directory = Path(reader._downloads._root) / tr.chat_dir_name(SUPERGROUP)
            directory.mkdir()
            conflict = directory / "合集 第1期 - 01 [21].mp4"
            conflict.write_bytes(b"unverified")
            first = await reader.download_media("https://t.me/chatroom/21")
            self.assertEqual(first["jobs"][0]["status"], "refused")
            gate.set()
            for job in first["jobs"][1:]:
                self.assertEqual((await reader._downloads.wait(job["job_id"]))["status"], "done")
            conflict.unlink()

            for msg_id in range(300, 479):
                self.client.by_id[msg_id] = fakes.message(
                    msg_id, NOW, "", SUPERGROUP, document=object(), payload=b"filler",
                    file=SimpleNamespace(name=f"file{msg_id}.zip", ext=".zip", size=6),
                )
                result = await reader.download_media("chatroom", message_id=msg_id)
                await reader._downloads.wait(result["jobs"][0]["job_id"])
            gate.clear()
            await self.queue_posts(reader, 19)
            self.assertEqual(len(reader.download_status()["jobs"]), 200)

            result = await reader.download_media("https://t.me/chatroom/21")
            self.assertEqual([job["message_id"] for job in result["jobs"]], [21, 22, 24])
            self.assertIn(result["jobs"][0]["status"], ("queued", "running"))
            self.assertEqual([job["status"] for job in result["jobs"][1:]], ["done", "done"])
            self.assertEqual(self.client.download_calls.count(22), 1)
            self.assertEqual(self.client.download_calls.count(24), 1)

    async def test_download_media_refuses_private_chats_before_fetching(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = tr.Reader(self.client, dl.Downloads(self.client, tmp))
            for ref in ("https://t.me/alice/1", "helper_bot"):
                with self.subTest(ref=ref), self.assertRaisesRegex(ToolError, "channels and groups"):
                    await reader.download_media(ref, message_id=1 if ref == "helper_bot" else 0)
            self.assertEqual(self.client.get_calls, [])
            self.assertEqual(self.client.download_calls, [])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_flood_wait_becomes_a_retry_hint(self):
        reader = tr.Reader(make_client(fail=errors.FloodWaitError(request=None, capture=42)))
        with self.assertRaisesRegex(ToolError, "retry in 42 s"):
            await reader.read_messages("newsfeed")


class ServeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.client = SimpleNamespace(
            connect=mock.AsyncMock(),
            is_user_authorized=mock.AsyncMock(return_value=True),
            disconnect=mock.AsyncMock(),
            disconnected=asyncio.get_running_loop().create_future(),
        )

        async def wait_disconnected():
            await self.client.disconnected

        self.client.wait_disconnected = wait_disconnected
        self.http = SimpleNamespace(should_exit=False, serve=self.serve_http)
        for patch in (
            mock.patch.object(tr, "make_client", return_value=self.client),
            mock.patch.object(tr, "_Server", return_value=self.http),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.task = None
        self.addAsyncCleanup(self.stop_task)

    async def serve_http(self):
        self.started.set()
        try:
            while not self.http.should_exit:
                await asyncio.sleep(0.001)
        finally:
            self.stopped.set()

    async def start(self):
        self.task = asyncio.create_task(tr.serve(9520))
        await asyncio.wait_for(self.started.wait(), 1)

    async def stop_task(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.client.disconnected.done() and not self.client.disconnected.cancelled():
            self.client.disconnected.exception()

    async def test_permanent_disconnect_stops_http_and_returns_failure(self):
        await self.start()
        self.client.disconnected.set_result(None)
        self.assertEqual(await asyncio.wait_for(self.task, 1), 1)
        self.assertTrue(self.stopped.is_set())
        self.client.disconnect.assert_awaited_once()

    async def test_transport_failure_stops_http_and_returns_failure(self):
        await self.start()
        self.client.disconnected.set_exception(ConnectionError("connection lost"))
        self.assertEqual(await asyncio.wait_for(self.task, 1), 1)
        self.assertTrue(self.stopped.is_set())
        self.client.disconnect.assert_awaited_once()

    async def test_revoked_authorization_keeps_the_no_restart_exit_code(self):
        await self.start()
        self.client.disconnected.set_exception(errors.SessionRevokedError(request=None))
        self.assertEqual(await asyncio.wait_for(self.task, 1), tr.EXIT_NOT_LOGGED_IN)
        self.assertTrue(self.stopped.is_set())
        self.client.disconnect.assert_awaited_once()

    async def test_http_shutdown_disconnects_the_client_and_exits_zero(self):
        await self.start()
        self.http.should_exit = True
        self.assertEqual(await asyncio.wait_for(self.task, 1), 0)
        self.client.disconnect.assert_awaited_once()

    async def test_simultaneous_http_shutdown_consumes_the_disconnect_error(self):
        async def finish_both():
            self.client.disconnected.set_exception(errors.SessionRevokedError(request=None))

        self.http.serve = finish_both
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            self.task = asyncio.create_task(tr.serve(9520))
            self.assertEqual(await asyncio.wait_for(self.task, 1), 0)
            self.client.disconnected = loop.create_future()
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(previous_handler)
        self.client.disconnect.assert_awaited_once()

    async def test_http_errors_are_not_swallowed(self):
        async def fail_http():
            raise RuntimeError("HTTP failed")

        self.http.serve = fail_http
        self.task = asyncio.create_task(tr.serve(9520))
        with self.assertRaisesRegex(RuntimeError, "HTTP failed"):
            await self.task
        self.client.disconnect.assert_awaited_once()

    async def test_disconnect_does_not_wait_forever_for_an_incomplete_http_body(self):
        with socket.socket() as listener:
            listener.bind((tr.HOST, 0))
            port = listener.getsockname()[1]
            servers = []
            configured_timeouts = []

            class HTTPServer(tr.uvicorn.Server):
                async def serve(self):
                    await super().serve(sockets=[listener])

            def make_http(config):
                configured_timeouts.append(config.timeout_graceful_shutdown)
                # Keep an unbounded configuration unbounded so the regression still fails.
                if config.timeout_graceful_shutdown is not None:
                    config.timeout_graceful_shutdown = 0.05
                server = HTTPServer(config)
                servers.append(server)
                return server

            with mock.patch.object(tr, "_Server", side_effect=make_http):
                self.task = asyncio.create_task(tr.serve(port))
                writer = None
                try:
                    async with asyncio.timeout(3):
                        while not servers or not servers[0].started:
                            await asyncio.sleep(0.01)
                    _, writer = await asyncio.open_connection(tr.HOST, port)
                    writer.write((
                        f"POST /mcp HTTP/1.1\r\nHost: {tr.HOST}:{port}\r\n"
                        "Content-Type: application/json\r\n"
                        "Accept: application/json, text/event-stream\r\n"
                        "Content-Length: 100\r\n\r\n{"
                    ).encode())
                    await writer.drain()
                    async with asyncio.timeout(3):
                        while not servers[0].server_state.tasks:
                            await asyncio.sleep(0.01)
                    self.client.disconnected.set_result(None)
                    done, _ = await asyncio.wait({self.task}, timeout=1)
                    self.assertIn(self.task, done)
                    self.assertEqual(self.task.result(), 1)
                    self.client.disconnect.assert_awaited_once()
                    self.assertEqual(configured_timeouts, [10])
                finally:
                    if writer is not None:
                        writer.close()
                        await writer.wait_closed()
                    await asyncio.wait_for(self.task, 3)

    async def test_cancellation_stops_http_before_disconnecting(self):
        await self.start()
        self.client.disconnect.side_effect = lambda: self.assertTrue(self.stopped.is_set())
        self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.assertTrue(self.stopped.is_set())
        self.client.disconnect.assert_awaited_once()

    async def test_missing_authorization_returns_without_serving_http(self):
        self.client.is_user_authorized.return_value = False
        self.assertEqual(await tr.serve(9520), tr.EXIT_NOT_LOGGED_IN)
        self.assertFalse(self.started.is_set())
        self.client.disconnect.assert_awaited_once()


class DCMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "api_id").write_text("12345")
        (root / "api_hash").write_text("0" * 32)
        with mock.patch.dict(os.environ, {
            "TELEGRAM_API_ID_FILE": str(root / "api_id"),
            "TELEGRAM_API_HASH_FILE": str(root / "api_hash"),
            "TELEGRAM_READER_SESSION": str(root / "reader"),
        }, clear=True):
            self.client = tr.make_client()
        self.sender = self.client._sender
        self.connection = SimpleNamespace(disconnect=mock.AsyncMock())
        self.sender._try_connect = mock.AsyncMock(return_value=True)
        self.sender._try_gen_auth_key = mock.AsyncMock(return_value=True)
        self.sender._send_loop = mock.AsyncMock()
        self.sender._recv_loop = mock.AsyncMock()
        await self.sender.connect(self.connection)
        self.client.connect = mock.AsyncMock()
        self.client.is_user_authorized = mock.AsyncMock(return_value=True)
        self.client._get_dc = mock.AsyncMock(return_value=SimpleNamespace(
            id=2, ip_address="127.0.0.2", port=443,
        ))
        self.started = asyncio.Event()
        self.http = SimpleNamespace(should_exit=False, serve=self.serve_http)
        self.download_root = root / "downloads"
        self.download_root.mkdir()
        self.downloads = dl.Downloads(self.client, str(self.download_root))
        self.patches = [
            mock.patch.object(tr, "make_client", return_value=self.client),
            mock.patch.object(tr, "_Server", return_value=self.http),
            mock.patch.object(tr, "Downloads", return_value=self.downloads),
            mock.patch.dict(os.environ, {"TELEGRAM_READER_DOWNLOADS": str(self.download_root)}, clear=True),
        ]
        for patch in self.patches:
            patch.start()
        self.migrations = []
        self.task = asyncio.create_task(tr.serve(9520))
        await asyncio.wait_for(self.started.wait(), 1)

    async def serve_http(self):
        self.started.set()
        while not self.http.should_exit:
            await asyncio.sleep(0.001)

    async def asyncTearDown(self):
        for task in self.migrations:
            task.cancel()
        await asyncio.gather(*self.migrations, return_exceptions=True)
        self.http.should_exit = True
        await asyncio.wait_for(self.task, 3)
        jobs = [job.task for job in self.downloads._jobs.values() if job.task is not None]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.sender.disconnect()
        self.client.session.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    async def test_migration_gap_keeps_http_and_a_background_job(self):
        reconnecting = asyncio.Event()
        resume = asyncio.Event()
        download_gate = asyncio.Event()

        async def reconnect():
            reconnecting.set()
            await resume.wait()
            await self.sender.connect(self.connection)

        async def download(msg, file, progress_callback):
            await download_gate.wait()

        self.client.connect.side_effect = reconnect
        with mock.patch.object(self.client, "download_media", side_effect=download):
            message = SimpleNamespace(id=7, document=True, file=SimpleNamespace(
                name="file.zip", ext=".zip", size=7,
            ))
            job = await self.downloads.start("channel:7", "channel", "Channel", message)
            background = self.downloads._jobs[job["job_id"]].task
            migration = asyncio.create_task(self.client._switch_dc(2))
            self.migrations.append(migration)
            await asyncio.wait_for(reconnecting.wait(), 1)
            await asyncio.wait({self.task}, timeout=0.05)
            self.assertFalse(self.http.should_exit)
            self.assertFalse(self.task.done())
            self.assertFalse(background.done())
            resume.set()
            await asyncio.wait_for(migration, 1)
            self.assertFalse(self.http.should_exit)
            self.assertIs(self.downloads._jobs[job["job_id"]].task, background)

    async def test_fast_migration_reobserves_the_next_permanent_disconnect(self):
        async def reconnect():
            await self.sender.connect(self.connection)

        self.client.connect.side_effect = reconnect
        await self.client._switch_dc(2)
        await asyncio.wait({self.task}, timeout=0.05)
        self.assertFalse(self.http.should_exit)
        await self.sender.disconnect()
        self.assertEqual(await asyncio.wait_for(self.task, 1), 1)

    async def test_update_loop_revocation_preserves_the_no_restart_exit_code(self):
        error = errors.SessionRevokedError(request=None)
        self.client._authorized = True
        self.client._message_box = SimpleNamespace(
            is_empty=lambda: False,
            get_difference=lambda: object(),
            end_difference=lambda: None,
        )
        disconnected = self.client.disconnected
        with (
            mock.patch.object(self.client, "_save_states_and_entities", new=mock.AsyncMock()),
            mock.patch.object(type(self.client), "__call__", new=mock.AsyncMock(side_effect=error)),
        ):
            self.client._updates_handle = asyncio.create_task(self.client._update_loop())
            done, _ = await asyncio.wait({self.task}, timeout=1)
            self.assertIn(self.task, done)
            self.assertIs(self.client._updates_error, error)
            self.assertIsNone(disconnected.result())
            self.assertEqual(self.task.result(), tr.EXIT_NOT_LOGGED_IN)

    async def test_failed_initialization_with_a_connected_sender_still_exits(self):
        async def reconnect():
            await self.sender.connect(self.connection)
            raise ConnectionError("migration initialization failed")

        self.client.connect.side_effect = reconnect
        with self.assertRaisesRegex(ConnectionError, "migration initialization failed"):
            await self.client._switch_dc(2)
        self.assertTrue(self.client.is_connected())
        done, _ = await asyncio.wait({self.task}, timeout=1)
        self.assertIn(self.task, done)
        self.assertEqual(self.task.result(), 1)

    async def test_cancelled_migration_with_a_connected_sender_still_exits(self):
        connected = asyncio.Event()

        async def reconnect():
            await self.sender.connect(self.connection)
            connected.set()
            await asyncio.Event().wait()

        self.client.connect.side_effect = reconnect
        migration = asyncio.create_task(self.client._switch_dc(2))
        self.migrations.append(migration)
        await asyncio.wait_for(connected.wait(), 1)
        migration.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await migration
        self.assertTrue(self.client.is_connected())
        done, _ = await asyncio.wait({self.task}, timeout=1)
        self.assertIn(self.task, done)
        self.assertEqual(self.task.result(), 1)

    async def test_overlapping_migrations_wait_for_both_initializations(self):
        connected = [asyncio.Event(), asyncio.Event()]
        resume = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def reconnect():
            nonlocal calls
            index = calls
            calls += 1
            await self.sender.connect(self.connection)
            connected[index].set()
            await resume[index].wait()
            if index == 0:
                raise ConnectionError("first migration initialization failed")

        self.client.connect.side_effect = reconnect
        first = asyncio.create_task(self.client._switch_dc(2))
        self.migrations.append(first)
        await asyncio.wait_for(connected[0].wait(), 1)
        second = asyncio.create_task(self.client._switch_dc(3))
        self.migrations.append(second)
        await asyncio.wait_for(connected[1].wait(), 1)
        resume[1].set()
        await asyncio.wait_for(second, 1)
        await asyncio.wait({self.task}, timeout=0.05)
        self.assertFalse(self.http.should_exit)
        resume[0].set()
        with self.assertRaisesRegex(ConnectionError, "first migration initialization failed"):
            await asyncio.wait_for(first, 1)
        done, _ = await asyncio.wait({self.task}, timeout=1)
        self.assertIn(self.task, done)
        self.assertEqual(self.task.result(), 1)

    async def test_nested_migration_does_not_deadlock(self):
        nested = asyncio.Event()
        resume = asyncio.Event()
        calls = 0

        async def reconnect():
            nonlocal calls
            calls += 1
            await self.sender.connect(self.connection)
            if calls == 1:
                await self.client._switch_dc(3)
            else:
                nested.set()
                await resume.wait()

        self.client.connect.side_effect = reconnect
        migration = asyncio.create_task(self.client._switch_dc(2))
        self.migrations.append(migration)
        await asyncio.wait_for(nested.wait(), 1)
        await asyncio.wait({self.task}, timeout=0.05)
        self.assertFalse(self.http.should_exit)
        resume.set()
        await asyncio.wait_for(migration, 1)
        self.assertFalse(self.http.should_exit)


class FindAndCheckTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reader = tr.Reader(self.client, dl.Downloads(self.client, self.tmp.name, part_hashes=fakes.part_hashes))

    async def test_find_media_returns_this_chats_posts_with_the_given_sizes(self):
        found = await self.reader.find_media("@chatroom", [7, 1, 999])
        # 25 has the same size but belongs to a private chat, so it never shows up.
        self.assertEqual([m["id"] for m in found["messages"]], [24, 23, 22, 21, 13])
        self.assertEqual(found["messages"][-1]["media"]["name"], "pack.zip")
        self.assertIsNone(found["next_before_id"])
        self.assertEqual(found["notice"], tr.NOTICE)

    async def test_find_media_pages_through_the_history(self):
        with mock.patch.object(tr, "FIND_SCAN_LIMIT", 4):
            first = await self.reader.find_media("@chatroom", [1, 7])
            self.assertEqual(([m["id"] for m in first["messages"]], first["next_before_id"]), ([24, 23, 22], 22))
            rest = await self.reader.find_media("@chatroom", [1, 7], before_id=first["next_before_id"])
            # A full page cannot tell whether older messages exist, so one more call ends it.
            last = await self.reader.find_media("@chatroom", [1, 7], before_id=rest["next_before_id"])
        self.assertEqual(([m["id"] for m in rest["messages"]], rest["next_before_id"]), ([21, 13], 11))
        self.assertEqual((last["messages"], last["scanned"], last["next_before_id"]), ([], 0, None))

    async def test_find_media_refuses_bad_requests_before_reading_history(self):
        for chat, sizes, message in [
            ("@chatroom", [], "sizes is empty"),
            ("@chatroom", [0, -3], "sizes is empty"),
            ("@chatroom", list(range(1, tr.MAX_FIND_SIZES + 2)), "at most"),
            ("@alice", [7], "only channels and groups"),
        ]:
            with self.subTest(chat=chat, sizes=sizes[:3]):
                with self.assertRaisesRegex(ToolError, message):
                    await self.reader.find_media(chat, sizes)
        self.assertEqual(self.client.iter_calls, [])

    async def test_check_file_compares_a_local_file_with_the_post(self):
        (self.root / "pack.zip").write_bytes(b"zipdata")
        (self.root / "other.zip").write_bytes(b"zipDATA")
        same = await self.reader.check_file(str(self.root / "pack.zip"), "https://t.me/chatroom/13")
        self.assertEqual((same["match"], same["size"], same["message"]["id"]), (True, 7, 13))
        other = await self.reader.check_file("other.zip", "chatroom", message_id=13)
        self.assertFalse(other["match"])
        self.assertEqual((self.root / "other.zip").read_bytes(), b"zipDATA")

    async def test_check_file_refuses_private_chats_and_outside_paths(self):
        (self.root / "pack.zip").write_bytes(b"zipdata")
        with self.assertRaisesRegex(ToolError, "only channels and groups"):
            await self.reader.check_file("missing.zip", "alice", message_id=1)
        with self.assertRaisesRegex(ToolError, "inside"):
            await self.reader.check_file("/etc/hostname", "https://t.me/chatroom/13")
        with self.assertRaisesRegex(ToolError, "no file"):
            await self.reader.check_file("pack.zip", "https://t.me/newsfeed/3")
        no_downloads = tr.Reader(self.client)
        with self.assertRaisesRegex(ToolError, "not configured"):
            await no_downloads.check_file("pack.zip", "https://t.me/chatroom/13")


class SessionFileTest(unittest.TestCase):
    def test_the_session_file_is_readable_by_its_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "api_id").write_text("12345\n")
            Path(tmp, "api_hash").write_text("0" * 32 + "\n")
            env = {
                "TELEGRAM_API_ID_FILE": f"{tmp}/api_id",
                "TELEGRAM_API_HASH_FILE": f"{tmp}/api_hash",
                "TELEGRAM_READER_SESSION": f"{tmp}/reader",
            }
            previous = os.umask(0o022)
            try:
                with mock.patch.dict(os.environ, env):
                    tr.make_client().session.close()
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(os.stat(f"{tmp}/reader.session").st_mode), 0o600)


class ServerSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.server = tr.build_server(tr.Reader(make_client()), port=9520)

    def test_only_read_and_download_tools_are_exposed(self):
        tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}
        self.assertEqual(
            set(tools),
            {
                "list_chats", "read_messages", "search_messages", "get_message",
                "download_media", "download_status", "find_media", "check_file",
            },
        )
        writers = {name for name, tool in tools.items() if not tool.annotations.readOnlyHint}
        self.assertEqual(writers, {"download_media"})

    def test_photo_messages_come_back_as_an_image_block(self):
        content = asyncio.run(self.server.call_tool("get_message", {"ref": "https://t.me/chatroom/11"}))
        self.assertEqual([type(c).__name__ for c in content], ["TextContent", "ImageContent"])
        self.assertEqual(content[1].mimeType, "image/jpeg")
        self.assertEqual(json.loads(content[0].text)["notice"], tr.NOTICE)
        content = asyncio.run(
            self.server.call_tool("get_message", {"ref": "https://t.me/chatroom/11", "include_photo": False})
        )
        self.assertEqual([type(c).__name__ for c in content], ["TextContent"])

    def test_tools_accept_the_integer_ids_list_chats_returns(self):
        args = {"chat": marked(PUBLIC_CHANNEL), "limit": 2}
        content = asyncio.run(self.server.call_tool("read_messages", args))
        self.assertEqual([m["id"] for m in json.loads(content[0].text)["messages"]], [10, 9])


if __name__ == "__main__":
    unittest.main()
