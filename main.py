import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config import BOT_TOKEN
from modules.queue_manager import queue_manager
from modules.cleanup import run_cleanup_loop
from modules.safe_forward import set_bot_username
from modules.daily_reset_notifier import run_daily_reset_loop
from modules.premium_expiry import run_premium_expiry_loop
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
import handlers.terabox_handler


async def post_init(application: Application) -> None:
    await queue_manager.start()
    asyncio.create_task(run_cleanup_loop())
    asyncio.create_task(run_daily_reset_loop(application.bot))
    asyncio.create_task(run_premium_expiry_loop(application.bot))
    me = await application.bot.get_me()
    set_bot_username(me.username)
    logger.info(f"Bot started: @{me.username} (id={me.id})")
    print(f"✅ Bot running: @{me.username}")


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    handlers.start.setup(application)
    handlers.auth_handler.setup(application)
    handlers.forward_handler.setup(application)
    handlers.status_handler.setup(application)
    handlers.payment_handler.setup(application)
    handlers.help_handler.setup(application)
    handlers.admin_handler.setup(application)
    handlers.info_handler.setup(application)
    handlers.queue_handler.setup(application)
    handlers.terabox_handler.setup(application)

    logger.info("Starting polling...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
