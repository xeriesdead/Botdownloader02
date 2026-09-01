---
name: Telegram resolved chat IDs
description: Pyrogram message-fetch behavior for public Telegram usernames
---

For Telegram downloads, resolve a public username first and pass the returned numeric chat ID through message and album metadata fetches. Treat Telegram's `?single` query as an explicit single-message request. Prefer raw MTProto `channels.getMessages`/`messages.getMessages` when the Pyrogram wrapper hangs.

**Why:** Repeating a username lookup in Pyrogram can enter a slow or non-returning network path even after the channel was already resolved successfully; album metadata calls can hit the same path, so they also need a bounded timeout.

**How to apply:** Keep the resolved numeric ID through message-fetch and metadata stages, honor `?single` before album expansion, use Bot API copies for public albums when possible, bound `get_media_group`, and retain a compatible wrapper fallback. Use the original username only for Bot API copy/forward calls that require it.