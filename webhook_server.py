"""
Entry point untuk mode webhook (serverless), dipakai saat deploy ke platform
seperti Google Cloud Run — sebagai lawan dari mode polling di `main.py` yang
butuh proses selalu hidup.

Perbedaan dari main.py:
- Telegram update diterima lewat HTTP POST /webhook/<WEBHOOK_SECRET>,
  bukan lewat long-polling ke API Telegram.
- 3 loop latar belakang (cleanup, daily reset, premium expiry) diganti jadi
  endpoint HTTP /tasks/*, dipicu scheduler eksternal (mis. Google Cloud
  Scheduler) alih-alih loop yang menunggu di memori selamanya.

Lihat DEPLOY.md untuk cara deploy & konfigurasi scheduler.
"""

import os

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from config import BOT_TOKEN
from modules.queue_manager import queue_manager
from modules.safe_forward import set_bot_username
from modules.cleanup import run_cleanup_once
from modules.daily_reset_notifier import run_daily_reset_once
from modules.premium_expiry import run_premium_expiry_once
from logger import logger

import handlers.start
import handlers.auth_handler
import handlers.forward_handler
import handlers.status_handler
import handlers.payment_handler
import handlers.help_handler
import handlers.admin_handler
import handlers.info_handler
import handlers.queue_handler


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Environment variable '{key}' belum diset. "
            "Wajib untuk mode webhook — lihat DEPLOY.md."
        )
    return value


# Segmen path rahasia untuk URL webhook (mis. https://host/webhook/<WEBHOOK_SECRET>).
# Telegram tidak mengautentikasi request webhook-nya sendiri, jadi path ini
# yang mencegah pihak lain mengirim update palsu.
WEBHOOK_SECRET = _require_env("WEBHOOK_SECRET")

# Header rahasia yang wajib disertakan scheduler eksternal saat memanggil
# endpoint /tasks/* (X-Tasks-Secret).
TASKS_SECRET = _require_env("TASKS_SECRET")

PORT = int(os.getenv("PORT", "8080"))


def build_telegram_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    handlers.start.setup(application)
    handlers.auth_handler.setup(application)
    handlers.forward_handler.setup(application)
    handlers.status_handler.setup(application)
    handlers.payment_handler.setup(application)
    handlers.help_handler.setup(application)
    handlers.admin_handler.setup(application)
    handlers.info_handler.setup(application)
    handlers.queue_handler.setup(application)

    return application


def _tasks_secret_ok(request: web.Request) -> bool:
    return request.headers.get("X-Tasks-Secret") == TASKS_SECRET


async def handle_webhook(request: web.Request) -> web.Response:
    if request.match_info.get("secret") != WEBHOOK_SECRET:
        # Sengaja balas 404 (bukan 403) supaya tidak membocorkan bahwa path
        # webhook itu ada ke pihak yang menebak-nebak.
        return web.Response(status=404)

    application: Application = request.app["telegram_app"]
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(status=200)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_task_cleanup(request: web.Request) -> web.Response:
    if not _tasks_secret_ok(request):
        return web.Response(status=403)
    result = run_cleanup_once()
    return web.json_response(result)


async def handle_task_daily_reset(request: web.Request) -> web.Response:
    if not _tasks_secret_ok(request):
        return web.Response(status=403)
    application: Application = request.app["telegram_app"]
    total, notified = await run_daily_reset_once(application.bot)
    return web.json_response({"reset": total, "notified": notified})


async def handle_task_premium_expiry(request: web.Request) -> web.Response:
    if not _tasks_secret_ok(request):
        return web.Response(status=403)
    application: Application = request.app["telegram_app"]
    expired = await run_premium_expiry_once(application.bot)
    return web.json_response({"expired": expired})


async def on_startup(app: web.Application):
    application: Application = app["telegram_app"]
    await application.initialize()
    await application.start()
    await queue_manager.start()

    me = await application.bot.get_me()
    set_bot_username(me.username)
    logger.info(f"Bot started (webhook mode): @{me.username} (id={me.id})")
    print(f"✅ Bot running (webhook mode): @{me.username}")


async def on_cleanup(app: web.Application):
    application: Application = app["telegram_app"]
    await application.stop()
    await application.shutdown()


def create_app() -> web.Application:
    app = web.Application()
    app["telegram_app"] = build_telegram_application()

    app.router.add_post(f"/webhook/{{secret}}", handle_webhook)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/tasks/cleanup", handle_task_cleanup)
    app.router.add_post("/tasks/daily-reset", handle_task_daily_reset)
    app.router.add_post("/tasks/premium-expiry", handle_task_premium_expiry)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    app = create_app()
    logger.info(f"Starting webhook server on port {PORT}...")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
