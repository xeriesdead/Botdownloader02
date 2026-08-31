---
name: Telegram resolved chat IDs
description: Pyrogram message-fetch behavior for public Telegram usernames
---

For Telegram downloads, resolve a public username first and pass the returned numeric chat ID to `get_messages()` instead of passing the username again.

**Why:** Repeating a username lookup in Pyrogram can enter a slow or non-returning network path even after the channel was already resolved successfully.

**How to apply:** Keep the resolved numeric ID through the message-fetch and metadata stages; use the original username only for Bot API copy/forward calls that require it.