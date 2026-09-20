async def _safe_create_video_thumbnail_async(path: str, timeout_seconds: int = 20) -> str | None:
    """Buat thumbnail video dengan timeout yang jelas agar album tidak macet. """
    try:
        return await asyncio.wait_for(
            _create_video_thumbnail_async(path),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Thumbnail video timeout untuk %s setelah %ss; lanjut tanpa thumbnail.",
            path,
            timeout_seconds,
        )
        return None
    except Exception as exc:
        logger.warning("Thumbnail video gagal untuk %s: %s", path, exc)
        return None


async def _send_album_via_bot(client, bot, chat, msg_id: int, user_chat_id: int,
                              on_progress=None, is_premium: bool = False,
                              messages=None):
