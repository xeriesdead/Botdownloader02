---
name: Telegram resolved chat IDs
description: Pyrogram message-fetch behavior for public Telegram usernames
---

For Telegram downloads, resolve a public username first and pass the returned numeric chat ID through message and album metadata fetches. Treat Telegram's `?single` query as an explicit single-message request. Prefer raw MTProto `channels.getMessages`/`messages.getMessages` when the Pyrogram wrapper hangs.

**Why:** Repeating a username lookup in Pyrogram can enter a slow or non-returning network path even after the channel was already resolved successfully; album metadata calls can hit the same path, so they also need a bounded timeout.

**How to apply:** Keep the resolved numeric ID through message-fetch and metadata stages, honor `?single` before album expansion, use Bot API copies for public albums when possible, bound `get_media_group`, and retain a compatible wrapper fallback. Use `InputChannel` (not `InputPeerChannel`) for raw `channels.getMessages`; use the original username only for Bot API copy/forward calls that require it.

If Telegram omits a single media item's `file_size`, permit the transfer only through a path that enforces the account limit from transfer progress and the downloaded file size before upload. Keep unknown-size albums rejected until every item can be bounded safely.

**Why:** Rejecting a protected single item with unavailable metadata blocks a valid request, while blindly allowing it can bypass file-size policy or the Bot API upload limit.

**How to apply:** Apply this only to a single downloadable media item; never use metadata absence as proof that a file is small.