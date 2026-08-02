"""
AI features handler — Face Swap & Image Generation via Replicate.

Commands:
  /faceswap  — 2-step photo flow: target → source face → swapped result
  /imagine   — generate an image from a text prompt (Flux Schnell)
"""

import asyncio
import traceback

import replicate
from telegram.ext import CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

from modules.channel_guard import require_member
from config import BOT_TOKEN, REPLICATE_API_TOKEN
from logger import logger

# ---------------------------------------------------------------------------
# In-memory state for the 2-step face-swap flow
# uid -> {"step": "target" | "source", "target_file_id": str}
# ---------------------------------------------------------------------------
_faceswap_state: dict[int, dict] = {}

# Replicate model IDs
_FACESWAP_MODEL = "ddvinh1/inswapper:25bdae46f2713138640b6e8c04dc4ca18625ce95b1863936b053eee42d9ba6db"
_IMAGINE_MODEL  = "black-forest-labs/flux-schnell"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_tg_file_url(bot, file_id: str) -> str:
    """Return the direct HTTPS URL of a Telegram file (accessible from Replicate)."""
    tg_file = await bot.get_file(file_id)
    # tg_file.file_path is the path after /file/bot<token>/
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"


def _check_token() -> bool:
    """Return True if REPLICATE_API_TOKEN is set in the environment."""
    return bool(REPLICATE_API_TOKEN)


def _to_url(output) -> str:
    """
    Normalise Replicate output to a string URL.
    Handles plain strings, FileOutput objects, and lists.
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

    if not _check_token():
        await update.message.reply_text(
            "❌ Fitur AI belum aktif.\n"
            "Tambahkan <code>REPLICATE_API_TOKEN</code> ke Railway Variables "
            "(<a href='https://replicate.com'>replicate.com</a> → gratis).",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
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

    if not _check_token():
        await update.message.reply_text(
            "❌ Fitur AI belum aktif.\n"
            "Tambahkan <code>REPLICATE_API_TOKEN</code> ke Railway Variables "
            "(<a href='https://replicate.com'>replicate.com</a> → gratis).",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
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
        logger.info(f"imagine output url: {image_url}")

        await update.message.reply_photo(
            photo=image_url,
            caption=f"🎨 <b>{prompt}</b>",
            parse_mode=ParseMode.HTML,
        )
        await msg.delete()

    except Exception as e:
        logger.error(f"imagine error: {e}\n{traceback.format_exc()}")
        await msg.edit_text(
            f"❌ Gagal membuat gambar.\n<code>{type(e).__name__}: {e}</code>",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Photo message handler (handles the 2-step face-swap flow)
# ---------------------------------------------------------------------------

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
            # Get direct HTTPS URLs — more reliable than uploading bytes
            target_url, source_url = await asyncio.gather(
                _get_tg_file_url(context.bot, target_file_id),
                _get_tg_file_url(context.bot, photo.file_id),
            )
            logger.info(f"faceswap target_url={target_url} source_url={source_url}")

            output = await replicate.async_run(
                _FACESWAP_MODEL,
                input={
                    "target_img": target_url,   # image to swap face INTO
                    "source_img": source_url,   # face to use
                },
            )
            image_url = _to_url(output)
            logger.info(f"faceswap output url: {image_url}")

            await update.message.reply_photo(
                photo=image_url,
                caption="✅ <b>Face swap selesai!</b>",
                parse_mode=ParseMode.HTML,
            )
            await msg.delete()

        except Exception as e:
            logger.error(f"faceswap error: {e}\n{traceback.format_exc()}")
            await msg.edit_text(
                f"❌ Gagal melakukan face swap.\n<code>{type(e).__name__}: {e}</code>",
                parse_mode=ParseMode.HTML,
            )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def setup(app):
    app.add_handler(CommandHandler("faceswap",       _faceswap_cmd))
    app.add_handler(CommandHandler("cancelfaceswap", _cancel_faceswap_cmd))
    app.add_handler(CommandHandler("imagine",         _imagine_cmd))

    # group=1 so it runs after group=0 handlers
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE,
            _photo_handler,
        ),
        group=1,
    )
