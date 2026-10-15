"""Telethon stand-ins for the unit tests. They mimic only what telegram_reader calls."""
from types import SimpleNamespace

from telethon import utils
from telethon.tl import types

MEDIA_FIELDS = ("photo", "video", "voice", "audio", "sticker", "gif", "document", "web_preview", "poll", "geo", "contact")


def channel(id, title, username=None, broadcast=False, megagroup=False):
    return types.Channel(
        id=id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=broadcast,
        megagroup=megagroup,
        username=username,
    )


def message(id, date, text, chat, **fields):
    msg = SimpleNamespace(
        id=id,
        date=date,
        message=text,
        chat=chat,
        sender=None,
        entities=None,
        reply_markup=None,
        file=None,
        views=None,
        forwards=None,
        replies=None,
        edit_date=None,
        post_author=None,
        grouped_id=None,
        forward=None,
        reply_to_msg_id=None,
        peer_id=utils.get_peer(chat) if chat is not None else None,
        **{name: None for name in MEDIA_FIELDS if name not in fields},
    )
    for name, value in fields.items():
        setattr(msg, name, value)
    msg.media = next((getattr(msg, name) for name in MEDIA_FIELDS if getattr(msg, name)), None)
    return msg


def dialog(entity, marked_id, unread=0):
    return SimpleNamespace(entity=entity, id=marked_id, unread_count=unread, archived=False)


class FakeClient:
    def __init__(self, entities=None, history=None, dialogs=None, global_results=None, by_id=None, fail=None):
        self.entities = entities or {}
        self.history = history or {}
        self.dialogs = dialogs or []
        self.global_results = global_results or []
        # Messages that get_messages returns by id regardless of chat, to model ids
        # that basic groups share with private chats.
        self.by_id = by_id or {}
        # An exception every history call raises, to exercise error translation.
        self.fail = fail
        self.iter_calls = []
        self.get_calls = []
        self.download_calls = []

    async def get_entity(self, ref):
        try:
            return self.entities[ref]
        except KeyError:
            raise ValueError(f'Cannot find any entity corresponding to "{ref}"') from None

    async def iter_dialogs(self, **kwargs):
        for d in self.dialogs:
            yield d

    async def iter_messages(self, entity, limit=None, offset_id=0, min_id=0, search=None, **kwargs):
        self.iter_calls.append({"entity": entity, "limit": limit, "offset_id": offset_id, "min_id": min_id, "search": search})
        if self.fail:
            raise self.fail
        if entity is None:
            source = self.global_results
        else:
            source = sorted(self.history.get(entity.id, []), key=lambda m: m.id, reverse=True)
        produced = 0
        for msg in source:
            if offset_id and msg.id >= offset_id:
                continue
            if min_id and msg.id <= min_id:
                continue
            if search and search not in msg.message:
                continue
            if limit is not None and produced >= limit:
                return
            produced += 1
            yield msg

    async def download_media(self, msg, file=None, progress_callback=None):
        self.download_calls.append(msg.id)
        if file is bytes:
            return msg.payload
        file.write(msg.payload)
        if progress_callback:
            progress_callback(len(msg.payload), len(msg.payload))
        return file

    async def get_messages(self, entity, ids):
        self.get_calls.append({"entity": entity, "ids": ids})
        if isinstance(ids, list):
            return [self._lookup(entity, i) for i in ids]
        return self._lookup(entity, ids)

    def _lookup(self, entity, msg_id):
        if msg_id in self.by_id:
            return self.by_id[msg_id]
        return next((m for m in self.history.get(entity.id, []) if m.id == msg_id), None)
