---
name: Telegram resolved chat IDs
description: Pyrogram message-fetch behavior for public Telegram usernames
---

For Telegram downloads, resolve a public username first and pass the returned numeric chat ID through normal message and album metadata fetches. A `?single` link points to an item, but that item may belong to an album; keep the group signal and offer the user a choice between the full album and only the linked item. Prefer raw MTProto `channels.getMessages`/`messages.getMessages` when the Pyrogram wrapper hangs. If a public message still returns `MessageEmpty`, call `contacts.ResolveUsername` and use the returned `PeerChannel` plus matching channel's fresh `access_hash` directly; `resolve_peer(username)` can still return stale cached storage, and invoking `ResolveUsername` without using its response does not guarantee that storage was updated.

If the bot-backed public session still returns an empty message or unknown media size, retry the worker with the requesting user's logged-in session when available. Repeating the same raw request through the bot session does not inherit the user's channel membership.

**Why:** Repeating a username lookup in Pyrogram can enter a slow or non-returning network path even after the channel was already resolved successfully; album metadata calls can hit the same path, so they also need a bounded timeout. Separately, `resolve_peer()` reads its local peer cache first, so a new username call does not guarantee a fresh access hash when Telegram returns an empty message. A bot-backed MTProto session also has different channel access from the user's logged-in session.

**How to apply:** Keep the resolved numeric ID for wrapper and metadata requests, use `?single` to defer full-group metadata expansion while still preserving the album choice, use Bot API copies for public albums when possible, bound `get_media_group`, and retain a compatible wrapper fallback. On a public raw fallback after `MessageEmpty`, derive the raw channel input from the matching channel entity returned by `contacts.ResolveUsername`, within the existing hard timeout; only fall back to `resolve_peer()` if that response has no usable channel/access hash. Use `InputChannel` (not `InputPeerChannel`) for raw `channels.getMessages`; use the original username for Bot API copy/forward calls that require it. When a bot-backed public preflight cannot read a message or determine its media size, prefer the user's session for the queued worker, while retaining the bot session as fallback if the user is not logged in.

If Telegram omits a single media item's `file_size`, permit the transfer only through a path that enforces the account limit from transfer progress and the downloaded file size before upload. Keep unknown-size albums rejected until every item can be bounded safely.

**Why:** Rejecting a protected single item with unavailable metadata blocks a valid request, while blindly allowing it can bypass file-size policy or the Bot API upload limit.

**How to apply:** Apply this only to a single downloadable media item; never use metadata absence as proof that a file is small.