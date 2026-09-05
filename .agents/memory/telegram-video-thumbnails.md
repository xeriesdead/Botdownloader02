---
name: Telegram video thumbnails
description: Thumbnail handling for Telegram media re-uploaded through Pyrogram.
---

When a Telegram video arrives as a document, detect its video MIME type or file extension and re-upload it with a generated thumbnail; otherwise Telegram shows a plain document card.

**Why:** Protected/private media must be downloaded and re-uploaded, and the original Telegram preview metadata is not preserved by a raw document upload.

**How to apply:** Generate a small JPEG frame with the system ffmpeg when available, pass it as the Pyrogram/ Bot API video thumbnail, and keep sending the original file without a thumbnail if generation fails.