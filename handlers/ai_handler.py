"""
AI features handler.

  /faceswap  — Face Swap via Magic Hour (magichour.ai)
  /imagine   — Image Generation via HuggingFace Inference API (FLUX.1-schnell)

Railway Variables needed:
  MAGICHOUR_API_KEY  — from magichour.ai/developer  (10 credits/photo; 400 free on signup + 100/day)
  HF_TOKEN           — from huggingface.co/settings/tokens  ($0.10 free credits/month)
"""

import asyncio
import io
import os
import traceback

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.constants import ParseMode

from modules.channel_guard import require_member
from config import BOT_TOKEN
from logger import logger

# ---------------------------------------------------------------------------
# In-memory state  uid -> {step, gender, target_file_id}
# ---------------------------------------------------------------------------
_faceswap_state: dict[int, dict] = {}

_MH_BASE       = "https://api.magichour.ai"
_HF_IMAGINE_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mh_key() -> str | None:
    return os.getenv("MAGICHOUR_API_KEY")


def _hf_token() -> str | None:
    return os.getenv("HF_TOKEN")


async def _get_tg_file_url(bot, file_id: str) -> str:
    tg_file = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"


# ---------------------------------------------------------------------------
# Magic Hour — face swap  (async REST + polling)
# ---------------------------------------------------------------------------

async def _magichour_faceswap(target_url: str, source_url: str) -> str:
    """Submit face-swap job and poll until complete. Returns download URL."""
    api_key = _mh_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "name": "Telegram Bot Face Swap",
        "assets": {
            "face_swap_mode": "all-faces",
            "source_file_path": target_url,       # image to modify
            "face_mappings": [{"new_face": source_url}],  # face to use
        },
    }

    async with aiohttp.ClientSession() as session:
        # 1. Create the job
        async with session.post(
            f"{_MH_BASE}/v1/face-swap-photo",
            json=payload,
            headers=headers,
        ) as resp:
            data = await resp.json()
            logger.info(f"MH create {resp.status}: {data}")
            if resp.status not in (200, 201):
                raise Exception(f"MH create failed ({resp.status}): {data}")
            job_id = data["id"]

        # 2. Poll until complete (max ~3 min)
        for attempt in range(60):
            await asyncio.sleep(3)
            async with session.get(
                f"{_MH_BASE}/v1/image-projects/{job_id}",
                headers=headers,
            ) as resp:
                poll = await resp.json()
                status = poll.get("status", "unknown")
                logger.info(f"MH poll #{attempt + 1} status={status}")

                if status == "complete":
                    downloads = poll.get("downloads", [])
                    if not downloads:
                        raise Exception("MH job complete but downloads list is empty")
                    return downloads[0]["url"]
                if status in ("error", "canceled"):
                    raise Exception(f"MH job {status}: {poll.get('error', 'no detail')}")

    raise Exception("Face swap timed out after 3 minutes")


# ---------------------------------------------------------------------------
# HuggingFace — image generation  (async REST)
# ---------------------------------------------------------------------------

async def _hf_imagine(prompt: str) -> bytes:
    """Generate image with FLUX.1-schnell. Returns raw JPEG bytes."""
    headers = {
        "Authorization": f"Bearer {_hf_token()}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": prompt,
        "parameters": {"num_inference_steps": 4},
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(_HF_IMAGINE_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"HF {resp.status}: {text[:300]}")
            return await resp.read()  # raw image bytes


# ---------------------------------------------------------------------------
# /faceswap — Step 0: pick gender via inline keyboard
# ---------------------------------------------------------------------------

async def _faceswap_cmd(update, context):
    if not await require_member(context.bot, update):
        return

    if not _mh_key():
        await update.message.reply_text(
            "❌ Fitur face swap belum aktif.\n"
            "Tambahkan <code>MAGICHOUR_API_KEY</code> ke Railway Variables.\n"
            "Daftar gratis → <a href='https://magichour.ai/developer'>magichour.ai/developer</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Pria",    callback_data="fs_gender_male"),
            InlineKeyboardButton("👩 Wanita",  callback_data="fs_gender_female"),
            InlineKeyboardButton("🧑 Lainnya", callback_data="fs_gender_other"),
        ],
        [InlineKeyboardButton("❌ Batal", callback_data="fs_cancel")],
    ])
    await update.message.reply_text(
        "🔄 <b>Face Swap</b>\n\nPilih gender dari wajah sumber yang akan kamu pakai:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def _faceswap_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "fs_cancel":
        _faceswap_state.pop(uid, None)
        await query.edit_message_text("❌ Face swap dibatalkan.")
        return

    gender = query.data[len("fs_gender_"):]
    _faceswap_state[uid] = {"step": "target", "gender": gender}

    label = {"male": "👨 Pria", "female": "👩 Wanita"}.get(gender, "🧑 Lainnya")
    await query.edit_message_text(
        f"✅ Gender: <b>{label}</b>\n\n"
        "Langkah 1/2 — Kirim foto <b>target</b>\n"
        "<i>(foto yang wajahnya akan diganti)</i>\n\n"
        "Ketik /cancelfaceswap untuk batal.",
        parse_mode=ParseMode.HTML,
    )


async def _cancel_faceswap_cmd(update, context):
    uid = update.effective_user.id
    if _faceswap_state.pop(uid, None) is not None:
        await update.message.reply_text("✅ Face swap dibatalkan.")
    else:
        await update.message.reply_text("Tidak ada sesi face swap yang aktif.")


# ---------------------------------------------------------------------------
# /imagine
# ---------------------------------------------------------------------------

async def _imagine_cmd(update, context):
    if not await require_member(context.bot, update):
        return

    if not _hf_token():
        await update.message.reply_text(
            "❌ Fitur image generation belum aktif.\n"
            "Tambahkan <code>HF_TOKEN</code> ke Railway Variables.\n"
            "Daftar gratis → <a href='https://huggingface.co/settings/tokens'>huggingface.co/settings/tokens</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "❌ Tulis prompt setelah perintah.\n"
            "Contoh: <code>/imagine a dragon flying over Tokyo at sunset</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    msg = await update.message.reply_text("🎨 Membuat gambar, harap tunggu…")
    try:
        image_bytes = await _hf_imagine(prompt)
        await update.message.reply_photo(
            photo=io.BytesIO(image_bytes),
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
# Photo message handler — face swap 2-step photo flow
# ---------------------------------------------------------------------------

async def _photo_handler(update, context):
    uid = update.effective_user.id
    state = _faceswap_state.get(uid)
    if not state:
        return

    photo = update.message.photo[-1]  # largest size

    if state["step"] == "target":
        _faceswap_state[uid] = {**state, "step": "source", "target_file_id": photo.file_id}
        await update.message.reply_text(
            "✅ Foto target diterima!\n\n"
            "Langkah 2/2 — Kirim foto <b>sumber wajah</b>\n"
            "<i>(wajah yang akan dipasang ke foto target)</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if state["step"] == "source":
        target_file_id = state["target_file_id"]
        del _faceswap_state[uid]

        msg = await update.message.reply_text("⏳ Memproses face swap, harap tunggu…")
        try:
            target_url, source_url = await asyncio.gather(
                _get_tg_file_url(context.bot, target_file_id),
                _get_tg_file_url(context.bot, photo.file_id),
            )
            logger.info(f"faceswap target={target_url} source={source_url}")

            result_url = await _magichour_faceswap(target_url, source_url)
            logger.info(f"faceswap result={result_url}")

            await update.message.reply_photo(
                photo=result_url,
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
    app.add_handler(CommandHandler("imagine",        _imagine_cmd))
    app.add_handler(CallbackQueryHandler(_faceswap_callback, pattern=r"^fs_"))
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE,
            _photo_handler,
        ),
        group=1,
    )
