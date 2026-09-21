---
name: Album completion status
description: Why large Telegram album items can appear frozen at 98 percent
---

Large album downloads can finish while the visible status remains at 98%: the progress debounce may suppress the final 100% callback, and post-download thumbnail generation can take a long time without a phase update.

**Why:** Telegram transfer callbacks and status-message edits are separate operations; a stale progress message does not prove that the transfer is still downloading.

**How to apply:** Always allow an explicit final progress update, keep status callbacks from blocking the transfer callback, and skip custom ffmpeg thumbnails for very large videos so Telegram can generate the preview.

Album status is emitted immediately after the file download and before video thumbnail preparation, so a status such as “media 5/10 selesai diunduh” can indicate a thumbnail stall rather than a download stall.

**Why:** Thumbnail extraction is post-download work but appears under the same user-facing preparation message.

**How to apply:** Keep thumbnail extraction bounded and continue without a custom thumbnail when it exceeds its deadline; clean up any late thread result.

Progress callbacks need the same hard timeout as media operations; `asyncio.wait_for()` can remain stuck while waiting for a slow Telegram edit to cancel even after the edit text is visible.

**Why:** A status update can reach Telegram while its client coroutine is still unwinding, making the album appear frozen at the last visible progress message.

**How to apply:** Use a task-based hard timeout for status/progress callbacks, cancel without awaiting a potentially stuck callback, and consume its eventual result.