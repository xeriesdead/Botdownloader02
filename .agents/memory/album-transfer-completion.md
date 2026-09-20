---
name: Album completion status
description: Why large Telegram album items can appear frozen at 98 percent
---

Large album downloads can finish while the visible status remains at 98%: the progress debounce may suppress the final 100% callback, and post-download thumbnail generation can take a long time without a phase update.

**Why:** Telegram transfer callbacks and status-message edits are separate operations; a stale progress message does not prove that the transfer is still downloading.

**How to apply:** Always allow an explicit final progress update, keep status callbacks from blocking the transfer callback, and skip custom ffmpeg thumbnails for very large videos so Telegram can generate the preview.