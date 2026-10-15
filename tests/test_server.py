"""Unit tests for telegram_reader.server. Run: PYTHONPATH=src python -m unittest discover -s tests"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
        self.assertEqual(tr.parse_chat_ref(" newsfeed "), "newsfeed")

    def test_phone_numbers_and_special_names_never_reach_telethon(self):
        for bad in ("+8613800000000", "me", "  ", "news feed", "https://t.me/+AbCdEf"):
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
        for chat in ("@alice", "helper_bot", marked(INBOX)):
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


class ServerSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.server = tr.build_server(tr.Reader(make_client()), port=9520)

    def test_only_read_and_download_tools_are_exposed(self):
        tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}
        self.assertEqual(
            set(tools),
            {"list_chats", "read_messages", "search_messages", "get_message", "download_media", "download_status"},
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
