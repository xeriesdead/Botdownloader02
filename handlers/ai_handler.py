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


async def _download_tg_bytes(bot, file_id: str) -> bytes:
    """Download a Telegram photo as raw bytes."""
    tg_file = await bot.get_file(file_id, read_timeout=60, connect_timeout=30)
    data = await tg_file.download_as_bytearray(read_timeout=60, connect_timeout=30)
    return bytes(data)


# ---------------------------------------------------------------------------
# Magic Hour — face swap  (async REST: upload → create job → poll)
# ---------------------------------------------------------------------------

async def _mh_upload(session: aiohttp.ClientSession, api_key: str,
                     image_bytes: bytes) -> str:
    """Upload one image to Magic Hour storage. Returns file_path like 'api-assets/id/xxx.jpg'."""
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Step 1 — get pre-signed upload URL
    async with session.post(
        f"{_MH_BASE}/v1/files/upload-urls",
        json={"items": [{"type": "image", "extension": "jpg"}]},
        headers=auth_headers,
    ) as resp:
        data = await resp.json()
        logger.info(f"MH upload-urls {resp.status}: {data}")
        if resp.status not in (200, 201):
            raise Exception(f"MH upload-urls failed ({resp.status}): {data}")
        # Response may be a list OR wrapped: {"data": [...]}
        items_list = data if isinstance(data, list) else data.get("data", data.get("items", []))
        if not items_list:
            raise Exception(f"MH upload-urls unexpected response shape: {data}")
        item = items_list[0]
        upload_url = item["upload_url"]
        file_path  = item["file_path"]

    # Step 2 — PUT raw bytes to the pre-signed URL
    async with session.put(upload_url, data=image_bytes) as resp:
        if resp.status not in (200, 204):
            text = await resp.text()
            raise Exception(f"MH S3 PUT failed ({resp.status}): {text[:200]}")

    return file_path


async def _magichour_faceswap(target_bytes: bytes, source_bytes: bytes) -> str:
    """Upload both images, submit face-swap job, poll until complete. Returns download URL."""
    api_key = _mh_key()
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 120 s per individual HTTP call (upload to S3 can be slow for large photos)
    per_req_timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=per_req_timeout) as session:
        # 1. Upload both images to Magic Hour storage
        target_path, source_path = await asyncio.gather(
            _mh_upload(session, api_key, target_bytes),
            _mh_upload(session, api_key, source_bytes),
        )
        logger.info(f"MH uploaded target={target_path} source={source_path}")

        # 2. Create the face-swap job
        # target_file_path = photo whose face gets replaced
        # source_file_path = photo that provides the new face
        payload = {
            "name": "Telegram Bot Face Swap",
            "assets": {
                "target_file_path": target_path,
                "source_file_path": source_path,
            },
        }
        logger.info(f"MH create payload: {payload}")
        async with session.post(
            f"{_MH_BASE}/v1/face-swap-photo",
            json=payload,
            headers=auth_headers,
        ) as resp:
            data = await resp.json()
            logger.info(f"MH create {resp.status} full response: {data}")
            if resp.status not in (200, 201):
                raise Exception(f"MH create failed ({resp.status}): {data}")
            job_id = data["id"]

        # 3. Poll until complete (max ~3 min, 5 s interval)
        for attempt in range(36):
            await asyncio.sleep(5)
            try:
                async with session.get(
                    f"{_MH_BASE}/v1/image-projects/{job_id}",
                    headers=auth_headers,
                ) as resp:
                    poll = await resp.json()
            except asyncio.TimeoutError:
                logger.warning(f"MH poll #{attempt + 1} timed out, retrying…")
                continue

            status = poll.get("status", "unknown")
            logger.info(f"MH poll #{attempt + 1} status={status} full={poll}")

            if status == "complete":
                downloads = poll.get("downloads", [])
                logger.info(f"MH complete downloads={downloads}")
                if not downloads:
                    raise Exception("MH complete but no downloads")
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
            target_bytes, source_bytes = await asyncio.gather(
                _download_tg_bytes(context.bot, target_file_id),
                _download_tg_bytes(context.bot, photo.file_id),
            )
            logger.info("faceswap: images downloaded, uploading to Magic Hour…")

            result_url = await _magichour_faceswap(target_bytes, source_bytes)
            logger.info(f"faceswap result={result_url}")

            await update.message.reply_photo(
                photo=result_url,
                caption="✅ <b>Face swap selesai!</b>",
                parse_mode=ParseMode.HTML,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
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
