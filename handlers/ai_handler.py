"""
AI features handler — Face Swap & Image Generation via fal.ai.

Commands:
  /faceswap  — multi-step flow: pick gender → target photo → source face → result
  /imagine   — generate an image from a text prompt (Flux Schnell)

Env var required: FAL_KEY  (from https://fal.ai/dashboard/keys)
"""

import asyncio
import traceback
import os

import fal_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.constants import ParseMode

from modules.channel_guard import require_member
from logger import logger

# ---------------------------------------------------------------------------
# In-memory state for the multi-step face-swap flow
# uid -> {"step": "target"|"source", "gender": str, "target_file_id": str}
# ---------------------------------------------------------------------------
_faceswap_state: dict[int, dict] = {}

# fal.ai model IDs
_FACESWAP_MODEL = "easel-ai/advanced-face-swap"
_IMAGINE_MODEL  = "fal-ai/flux/schnell"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_fal_key() -> bool:
    return bool(os.getenv("FAL_KEY"))


async def _get_tg_file_url(bot, file_id: str) -> str:
    """Return direct HTTPS URL of a Telegram file (accessible from fal.ai)."""
    from config import BOT_TOKEN
    tg_file = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"


def _no_token_msg() -> str:
    return (
        "❌ Fitur AI belum aktif.\n"
        "Tambahkan <code>FAL_KEY</code> ke Railway Variables.\n"
        "Daftar gratis di <a href='https://fal.ai/dashboard/keys'>fal.ai/dashboard/keys</a>"
    )


# ---------------------------------------------------------------------------
# /faceswap — Step 0: ask gender via inline keyboard
# ---------------------------------------------------------------------------

async def _faceswap_cmd(update, context):
    if not await require_member(context.bot, update):
        return

    if not _check_fal_key():
        await update.message.reply_text(
            _no_token_msg(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Pria",    callback_data="fs_gender_male"),
            InlineKeyboardButton("👩 Wanita",  callback_data="fs_gender_female"),
            InlineKeyboardButton("🧑 Lainnya", callback_data="fs_gender_non-binary"),
        ],
        [InlineKeyboardButton("❌ Batal", callback_data="fs_cancel")],
    ])

    await update.message.reply_text(
        "🔄 <b>Face Swap</b>\n\n"
        "Pilih gender dari wajah yang akan kamu pakai:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Callback: gender selected (or cancel)
# ---------------------------------------------------------------------------

async def _faceswap_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "fs_cancel":
        _faceswap_state.pop(uid, None)
        await query.edit_message_text("❌ Face swap dibatalkan.")
        return

    # data format: "fs_gender_<value>"
    gender = query.data[len("fs_gender_"):]
    _faceswap_state[uid] = {"step": "target", "gender": gender}

    gender_label = {"male": "👨 Pria", "female": "👩 Wanita"}.get(gender, "🧑 Lainnya")
    await query.edit_message_text(
        f"✅ Gender: <b>{gender_label}</b>\n\n"
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

    if not _check_fal_key():
        await update.message.reply_text(
            _no_token_msg(),
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
        result = await fal_client.run_async(
            _IMAGINE_MODEL,
            arguments={
                "prompt": prompt,
                "num_inference_steps": 4,
                "image_size": "landscape_4_3",
            },
        )
        image_url = result["images"][0]["url"]
        logger.info(f"imagine output: {image_url}")

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
# Photo message handler (handles the 2-step face-swap photo flow)
# ---------------------------------------------------------------------------

async def _photo_handler(update, context):
    uid = update.effective_user.id
    state = _faceswap_state.get(uid)
    if not state:
        return

    photo = update.message.photo[-1]  # largest size

    # --- Step 1: received target image ---
    if state["step"] == "target":
        _faceswap_state[uid] = {
            "step": "source",
            "gender": state["gender"],
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
        gender = state["gender"]
        del _faceswap_state[uid]

        msg = await update.message.reply_text("⏳ Memproses face swap, harap tunggu…")

        try:
            target_url, source_url = await asyncio.gather(
                _get_tg_file_url(context.bot, target_file_id),
                _get_tg_file_url(context.bot, photo.file_id),
            )
            logger.info(f"faceswap gender={gender} target={target_url} source={source_url}")

            result = await fal_client.run_async(
                _FACESWAP_MODEL,
                arguments={
                    "face_image_0":  {"url": source_url},
                    "gender_0":      gender,
                    "target_image":  {"url": target_url},
                    "workflow_type": "user_hair",
                    "upscale":       True,
                },
            )
            image_url = result["images"][0]["url"]
            logger.info(f"faceswap output: {image_url}")

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

    # Inline keyboard callback for gender selection
    app.add_handler(
        CallbackQueryHandler(_faceswap_callback, pattern=r"^fs_"),
    )

    # Photo handler — group=1, only fires when user has active faceswap state
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE,
            _photo_handler,
        ),
        group=1,
    )
