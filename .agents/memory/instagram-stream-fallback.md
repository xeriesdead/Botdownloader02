---
name: Instagram CDN fallback
description: Why oversized Instagram media needs a direct CDN-link fallback.
---

Direct Instagram/CDN media URLs are the useful fallback when Telegram cannot upload a large video. They are temporary and should be captured from the downloader result before the temporary download directory is cleaned up.

**Why:** Telegram upload limits can prevent a valid Instagram video from being delivered even when the media was successfully resolved.

**How to apply:** Keep the direct URL available long enough to send a Telegram inline URL button, and never substitute the Instagram post page when a CDN URL is available.