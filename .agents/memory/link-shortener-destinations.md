---
name: Link shortener destinations
description: The user's intended output behavior when resolving Linkvertise-family links
---

For supported Linkvertise-family links, the bot should return the final destination regardless of its domain; it must not require the destination to be Rentry. On success, the bot's final message should contain only the destination URL.

**Why:** The user explicitly clarified that the goal is to get through the shortlink and that the final destination host does not matter.

**How to apply:** Keep source-link recognition scoped to supported shortlink hosts, allow safe HTTP/HTTPS destination URLs from the resolver, and preserve the single-message status-to-result flow.