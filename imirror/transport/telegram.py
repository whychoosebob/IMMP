from asyncio import sleep
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
import logging

from aiohttp import ClientSession, ClientResponseError, FormData
from voluptuous import Schema, Any, All, Optional, ALLOW_EXTRA

import imirror


log = logging.getLogger(__name__)


class _Schema(object):

    config = Schema({"token": str}, extra=ALLOW_EXTRA, required=True)

    user = Schema({"id": int,
                   Optional("username", default=None): Any(str, None),
                   "first_name": str,
                   Optional("last_name", default=None): Any(str, None)},
                  extra=ALLOW_EXTRA, required=True)

    entity = Schema({"type": str,
                     "offset": int,
                     "length": int},
                    extra=ALLOW_EXTRA, required=True)

    message = Schema({"message_id": int,
                      "chat": {"id": int},
                      "date": int,
                      Optional("from", default=None): Any(user, None),
                      Optional("text", default=None): Any(str, None),
                      Optional("entities", default=[]): [entity],
                      Optional("reply_to_message", default=None):
                          Any(lambda v: _Schema.message(v), None),
                      Optional("photo", default=[]): [{"file_id": str, "width": int}],
                      Optional("new_chat_members", default=[]): [user],
                      Optional("left_chat_member", default=None): Any(user, None),
                      Optional("new_chat_title", default=None): Any(str, None)},
                     extra=ALLOW_EXTRA, required=True)

    update = Schema({"update_id": int,
                     Optional(Any("message", "edited_message",
                                  "channel_post", "edited_channel_post")): message},
                    extra=ALLOW_EXTRA, required=True)

    file = Schema({"file_path": str}, extra=ALLOW_EXTRA, required=True)

    send = Schema({"message_id": int}, extra=ALLOW_EXTRA, required=True)

    def api(value, nested=All()):
        return Schema(Any({"ok": True,
                           "result": nested},
                          {"ok": False,
                           "description": str,
                           "error_code": int}),
                      extra=ALLOW_EXTRA, required=True)(value)


class TelegramAPIError(imirror.TransportError):
    """
    Generic error from the Telegram API.
    """


class _AvatarParser(HTMLParser):
    # Telegram doesn't provide API access to public avatar URLs (cdnX.telesco.pe).  Instead, we'll
    # inspect the og:image meta property on users' profile pages and cache it for later use.

    @classmethod
    def parse_avatar(cls, html):
        parser = cls()
        try:
            parser.feed(html)
        except StopIteration:
            pass
        return parser.url

    def __init__(self):
        super().__init__()
        self.url = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "og:image":
            self.url = attrs.get("content")
            # Stop processing the rest of the document.
            raise StopIteration


class TelegramUser(imirror.User):
    """
    User present in Telegram.
    """

    _avatar_cache = {}

    @classmethod
    async def get_avatar(cls, telegram, username):
        if not username:
            # Users without a public username can't be looked up.
            return None
        if username not in cls._avatar_cache:
            log.debug("Fetching avatar for username '{}'".format(username))
            async with telegram._session.get("https://t.me/{}".format(username)) as resp:
                html = await resp.text()
            cls._avatar_cache[username] = _AvatarParser.parse_avatar(html)
        return cls._avatar_cache[username]

    @classmethod
    async def from_user(cls, telegram, json):
        """
        Convert a user :class:`dict` (attached to a message) to a :class:`.User`.

        Args:
            telegram (.TelegramTransport):
                Related transport instance that provides the user.
            json (dict):
                Telegram API `User <https://core.telegram.org/bots/api#user>`_ object.

        Returns:
            .TelegramUser:
                Parsed user object.
        """
        if json is None:
            return None
        user = _Schema.user(json)
        return cls(id=user["id"],
                   username=user["username"],
                   real_name=" ".join(filter(None, [user["first_name"], user["last_name"]])),
                   avatar=(await cls.get_avatar(telegram, user["username"])),
                   raw=user)


class TelegramSegment(imirror.Segment):
    """
    Transport-friendly representation of Telegram message formatting.
    """

    @classmethod
    def to_html(cls, segment):
        """
        Convert a :class:`.Segment` into HTML suitable for Telegram's automatic parsing.

        Args:
            segment (.Segment)
                Message segment created by another transport.

        Returns:
            str:
                HTML-formatted string.
        """
        text = segment.text
        # Any form of tag nesting (e.g. bold inside italic) isn't supported, so at most one type of
        # formatting may apply for each segment.
        if segment.link:
            text = "<a href=\"{}\">{}</a>".format(segment.link, text)
        elif segment.pre:
            text = "<pre>{}</pre>".format(text)
        elif segment.code:
            text = "<code>{}</code>".format(text)
        elif segment.bold:
            text = "<b>{}</b>".format(text)
        elif segment.italic:
            text = "<i>{}</i>".format(text)
        return text


class TelegramRichText(imirror.RichText):
    """
    Wrapper for Telegram-specific parsing of formatting.
    """

    @classmethod
    def from_entities(cls, text, entities):
        """
        Convert a string annotated by Telegram's entities to :class:`.RichText`.

        Args:
            text (str):
                Plain text without formatting.
            entities (dict list):
                List of Telegram API `MessageEntity
                <https://core.telegram.org/bots/api#messageentity>`_ objects.

        Returns:
            .TelegramRichText:
                Parsed rich text container.
        """
        if text is None:
            return None
        changes = defaultdict(dict)
        for json in entities:
            entity = _Schema.entity(json)
            if entity["type"] not in ("bold", "italic", "code", "pre"):
                continue
            start = entity["offset"]
            end = start + entity["length"]
            changes[start][entity["type"]] = True
            changes[end][entity["type"]] = False
        segments = []
        points = list(changes.keys())
        # Iterate through text in change start/end pairs.
        for start, end in zip([0] + points, points + [len(text)]):
            if start == end:
                # Zero-length segment at the start or end, ignore it.
                continue
            segments.append(TelegramSegment(text[start:end], **changes[start]))
        return cls(segments)


class TelegramMessage(imirror.Message):
    """
    Message originating from Telegram.
    """

    @classmethod
    async def from_message(cls, telegram, json):
        """
        Convert an API message :class:`dict` to a :class:`.Message`.

        Args:
            telegram (.TelegramTransport):
                Related transport instance that provides the event.
            json (dict):
                Telegram API `message <https://core.telegram.org/bots/api#message>`_ object.

        Returns:
            .TelegramMessage:
                Parsed message object.
        """
        message = _Schema.message(json)
        text = None
        user = None
        action = False
        reply_to = None
        joined = None
        left = None
        attachments = []
        if message["from"]:
            user = await TelegramUser.from_user(telegram, message["from"])
        if message["new_chat_title"]:
            action = True
        if message["new_chat_members"]:
            joined = [(await TelegramUser.from_user(telegram, member))
                      for member in message["new_chat_members"]]
            action = True
        if message["left_chat_member"]:
            left = [await TelegramUser.from_user(telegram, message["left_chat_member"])]
            action = True
        if message["text"]:
            text = TelegramRichText.from_entities(message["text"], message["entities"])
        elif message["new_chat_title"]:
            text = TelegramRichText([TelegramSegment("changed group name to "),
                                     TelegramSegment(message["new_chat_title"], bold=True)])
        elif message["new_chat_members"]:
            if joined == [user]:
                text = TelegramRichText([TelegramSegment("joined group via invite link")])
            else:
                text = TelegramRichText([TelegramSegment("invited ")])
                for join in joined:
                    link = "https://t.me/{}".format(join.username) if join.username else None
                    text.append(TelegramSegment(join.real_name, bold=(not link), link=link),
                                TelegramSegment(", "))
                text = text[:-1]
        elif message["left_chat_member"]:
            if left == [user]:
                text = TelegramRichText([TelegramSegment("left group")])
            else:
                part = left[0]
                link = "https://t.me/{}".format(part.username) if part.username else None
                text = TelegramRichText([TelegramSegment("removed "),
                                         TelegramSegment(part.real_name,
                                                         bold=(not link), link=link)])
        if message["reply_to_message"]:
            reply_to = (await cls.from_message(telegram, message["reply_to_message"]))[1]
        if message["photo"]:
            # This is a list of resolutions, find the original sized one to return.
            photo = max(message["photo"], key=lambda photo: photo["height"])
            async with telegram._session.get("{}/getFile".format(telegram._base),
                                             params={"file_id": photo["file_id"]}) as resp:
                file_json = _Schema.api(await resp.json(), _Schema.file)
            url = ("https://api.telegram.org/file/bot{}/{}"
                   .format(telegram._token, file_json["result"]["file_path"]))
            attachments.append(imirror.File(type=imirror.File.Type.image, source=url))
        return (telegram.host.resolve_channel(telegram, message["chat"]["id"]),
                cls(id=message["message_id"],
                    at=datetime.fromtimestamp(message["date"]),
                    text=text,
                    user=user,
                    action=action,
                    reply_to=reply_to,
                    joined=joined,
                    left=left,
                    attachments=attachments,
                    raw=message))

    @classmethod
    async def from_update(cls, telegram, update):
        """
        Convert an API update :class:`dict` to a :class:`.Message`.

        Args:
            telegram (.TelegramTransport):
                Related transport instance that provides the event.
            update (dict):
                Telegram API `update <https://core.telegram.org/bots/api#update>`_ object.

        Returns:
            .TelegramMessage:
                Parsed message object.
        """
        for key in ("message", "channel_post"):
            if update.get(key):
                return await cls.from_message(telegram, update[key])
            elif update.get("edited_{}".format(key)):
                channel, msg = await cls.from_message(telegram, update["edited_{}".format(key)])
                # Messages are edited in-place, no new ID is issued.
                msg.original = msg.id
                return (channel, msg)


class TelegramTransport(imirror.Transport):
    """
    Transport for a `Telegram <https://telegram.org>`_ bot.

    Config
        token (str):
            Telegram API token for a bot user (obtained from ``@BotFather``).
    """

    def __init__(self, name, config, host):
        super().__init__(name, config, host)
        config = _Schema.config(config)
        self._token = config["token"]
        # Connection objects that need to be closed on disconnect.
        self._session = None
        # Update ID from which to retrieve the next batch.  Should be one higher than the max seen.
        self._offset = 0

    async def _api(self, endpoint, schema, **kwargs):
        url = "https://api.telegram.org/bot{}/{}".format(self._token, endpoint)
        async with self._session.post(url, **kwargs) as resp:
            try:
                resp.raise_for_status()
            except ClientResponseError as e:
                raise TelegramAPIError("Unexpected response code: {}".format(resp.status)) from e
            else:
                json = await resp.json()
            result = _Schema.api(json, schema)
            if not result["ok"]:
                raise TelegramAPIError(json["description"], json["error_code"])
            return result["result"]

    async def connect(self):
        await super().connect()
        self._session = ClientSession()

    async def disconnect(self):
        await super().disconnect()
        if self._session:
            log.debug("Closing session")
            await self._session.close()
            self._session = None
        self._offset = 0

    async def put(self, channel, msg):
        if msg.deleted:
            # TODO
            return []
        parts = []
        for attach in msg.attachments:
            if isinstance(attach, imirror.File) and attach.type == imirror.File.Type.image:
                # Upload an image file to Telegram in its own message.
                # Prefer a source URL if available, else fall back to re-uploading the file.
                data = FormData({"chat_id": str(channel.source)})
                if msg.user:
                    data.add_field("caption", "{} sent an image"
                                              .format(msg.user.real_name or msg.user.username))
                if attach.source:
                    data.add_field("photo", attach.source)
                else:
                    img_resp = await attach.get_content(self._session)
                    data.add_field("photo", img_resp.content, filename=attach.title or "photo")
                parts.append(("sendPhoto", data))
        if msg.text or msg.reply_to:
            rich = msg.render(quote_reply=True)
            text = "".join(TelegramSegment.to_html(segment) for segment in rich)
            parts.append(("sendMessage", {"chat_id": channel.source,
                                          "text": text,
                                          "parse_mode": "HTML"}))
        sent = []
        for endpoint, data in parts:
            result = await self._api(endpoint, _Schema.send, data=data)
            sent.append(result["message_id"])
        return sent

    async def get(self):
        while True:
            log.debug("Making long-poll request")
            params = {"offset": self._offset,
                      "timeout": 240}
            for retry in range(3):
                try:
                    result = await self._api("getUpdates", [_Schema.update], params=params)
                except TelegramAPIError:
                    log.debug("Unexpected response or timeout")
                    if retry == 2:
                        raise
                    else:
                        await sleep(3)
                else:
                    break
            for update in result:
                log.debug("Received a message")
                if any(key in update or "edited_{}".format(key) in update
                       for key in ("message", "channel_post")):
                    yield await TelegramMessage.from_update(self, update)
                self._offset = max(update["update_id"] + 1, self._offset)
