"""
AI features handler — Face Swap & Image Generation via Replicate.

Commands:
  /faceswap  — 2-step photo flow: target → source face → swapped result
  /imagine   — generate an image from a text prompt (Flux Schnell)
"""

import io
import asyncio

import replicate
from telegram.ext import CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

from modules.channel_guard import require_member
from logger import logger

# ---------------------------------------------------------------------------
# In-memory state for the 2-step face-swap flow
# uid -> {"step": "target" | "source", "target_file_id": str}
# ---------------------------------------------------------------------------
_faceswap_state: dict[int, dict] = {}

# Replicate model IDs
_FACESWAP_MODEL = "lucataco/faceswap"
_IMAGINE_MODEL  = "black-forest-labs/flux-schnell"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _download_tg_file(bot, file_id: str) -> io.BytesIO:
    """Download a Telegram file and return it as a BytesIO object."""
    tg_file = await bot.get_file(file_id)
    data = await tg_file.download_as_bytearray()
    buf = io.BytesIO(bytes(data))
    buf.name = "image.jpg"
    return buf


def _to_url(output) -> str:
    """
    Normalise Replicate output to a string URL.
    Handles both plain strings and replicate.helpers.FileOutput objects.
    """
    if isinstance(output, list):
        output = output[0]
    return str(output)


# ---------------------------------------------------------------------------
# /faceswap
# ---------------------------------------------------------------------------

async def _faceswap_cmd(update, context):
    if not await require_member(context.bot, update):
        return

    uid = update.effective_user.id
    _faceswap_state[uid] = {"step": "target"}

    await update.message.reply_text(
        "🔄 <b>Face Swap</b>\n\n"
        "Langkah 1/2 — Kirim foto <b>target</b>\n"
        "<i>(foto yang wajahnya akan diganti)</i>\n\n"
        "Ketik /cancelfaceswap untuk membatalkan.",
        parse_mode=ParseMode.HTML,
    )


async def _cancel_faceswap_cmd(update, context):
    uid = update.effective_user.id
    if uid in _faceswap_state:
        del _faceswap_state[uid]
        await update.message.reply_text("✅ Face swap dibatalkan.")
    else:
        await update.message.reply_text("Tidak ada sesi face swap yang aktif.")


# ---------------------------------------------------------------------------
# /imagine
# ---------------------------------------------------------------------------

async def _imagine_cmd(update, context):
    if not await require_member(context.bot, update):
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "❌ Tulis prompt setelah perintah.\n"
            "Contoh: <code>/imagine a futuristic city at night</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    msg = await update.message.reply_text("🎨 Membuat gambar, harap tunggu…")

    try:
        output = await replicate.async_run(
            _IMAGINE_MODEL,
            input={
                "prompt": prompt,
                "num_outputs": 1,
                "output_format": "jpg",
                "output_quality": 90,
            },
        )
        image_url = _to_url(output)

        await update.message.reply_photo(
            photo=image_url,
            caption=f"🎨 <b>{prompt}</b>",
            parse_mode=ParseMode.HTML,
        )
        await msg.delete()

    except replicate.exceptions.ReplicateError as e:
        logger.error(f"imagine replicate error: {e}")
        await msg.edit_text(
            "❌ Gagal membuat gambar — pastikan <code>REPLICATE_API_TOKEN</code> sudah diset.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"imagine error: {e}")
        await msg.edit_text("❌ Terjadi kesalahan. Coba lagi nanti.")


# ---------------------------------------------------------------------------
# Photo message handler (handles the 2-step face-swap flow)
# ---------------------------------------------------------------------------

def _user_in_faceswap(update, _context) -> bool:
    """Custom filter: only fire if this user has an active face-swap session."""
    uid = update.effective_user.id if update.effective_user else None
    return uid is not None and uid in _faceswap_state


async def _photo_handler(update, context):
    uid = update.effective_user.id
    state = _faceswap_state.get(uid)
    if not state:
        return

    photo = update.message.photo[-1]  # largest available size

    # --- Step 1: received target image ---
    if state["step"] == "target":
        _faceswap_state[uid] = {
            "step": "source",
            "target_file_id": photo.file_id,
        }
        await update.message.reply_text(
            "✅ Foto target diterima!\n\n"
            "Langkah 2/2 — Kirim foto <b>sumber wajah</b>\n"
            "<i>(wajah yang akan dipasang ke foto target)</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Step 2: received source face — run the swap ---
    if state["step"] == "source":
        target_file_id = state["target_file_id"]
        del _faceswap_state[uid]

        msg = await update.message.reply_text("⏳ Memproses face swap, harap tunggu…")

        try:
            target_buf, source_buf = await asyncio.gather(
                _download_tg_file(context.bot, target_file_id),
                _download_tg_file(context.bot, photo.file_id),
            )

            output = await replicate.async_run(
                _FACESWAP_MODEL,
                input={
                    "target_image": target_buf,
                    "swap_image":   source_buf,
                },
            )
            image_url = _to_url(output)

            await update.message.reply_photo(
                photo=image_url,
                caption="✅ <b>Face swap selesai!</b>",
                parse_mode=ParseMode.HTML,
            )
            await msg.delete()

        except replicate.exceptions.ReplicateError as e:
            logger.error(f"faceswap replicate error: {e}")
            await msg.edit_text(
                "❌ Gagal melakukan face swap — pastikan <code>REPLICATE_API_TOKEN</code> sudah diset.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"faceswap error: {e}")
            await msg.edit_text("❌ Terjadi kesalahan. Coba lagi nanti.")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def setup(app):
    app.add_handler(CommandHandler("faceswap",       _faceswap_cmd))
    app.add_handler(CommandHandler("cancelfaceswap", _cancel_faceswap_cmd))
    app.add_handler(CommandHandler("imagine",         _imagine_cmd))

    # group=1 so it runs after group=0 handlers; only fires for users in state
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE,
            _photo_handler,
        ),
        group=1,
    )
