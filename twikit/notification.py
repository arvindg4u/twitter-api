from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client.client import Client
    from .tweet import Tweet
    from .user import User


class Notification:
    """
    Attributes
    ----------
    id : :class:`str`
        The unique identifier of the notification.
    timestamp_ms : :class:`int`
        The timestamp of the notification in milliseconds.
    icon : :class:`dict`
        Dictionary containing icon data for the notification.
    message : :class:`str`
        The message text of the notification.
    tweet : :class:`.Tweet`
        The tweet associated with the notification.
    from_user : :class:`.User`
        The user who triggered the notification.
    """
    def __init__(
        self, client: Client, data: dict, tweet: Tweet, from_user: User
    ) -> None:
        self._client = client
        self.tweet = tweet
        self.from_user = from_user

        # X has changed the notification payload several times; newer/rarer
        # notification types omit `message`, `icon` or `timestampMs`. Treat
        # every field as optional so one odd entry can't abort the whole
        # `get_notifications` call.
        self.id: str = str(data.get('id', ''))
        self.timestamp_ms: int = int(data.get('timestampMs') or 0)
        self.icon: dict = data.get('icon') or {}
        message = data.get('message') or {}
        if isinstance(message, dict):
            self.message: str = message.get('text', '') or ''
        else:
            self.message = str(message)

    def __eq__(self, __value: object) -> bool:
        return isinstance(__value, Notification) and self.id == __value.id

    def __ne__(self, __value: object) -> bool:
        return not self == __value

    def __repr__(self) -> str:
        return f'<Notification id="{self.id}">'
