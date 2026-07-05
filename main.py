"""Entrypoint: runs the Telegram bot and the dashboard server together in
one process via asyncio, as required for a single `bash run.sh` launch."""
import asyncio
import logging

import uvicorn

import bot
import config
import db
from server import app

logging.basicConfig(level=logging.INFO)


async def run_server():
    uvicorn_config = uvicorn.Config(
        app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)
    await server.serve()


async def main():
    db.init_db()

    tasks = [asyncio.create_task(run_server())]
    if config.TELEGRAM_BOT_TOKEN:
        application = bot.build_application()
        tasks.append(asyncio.create_task(bot.run_bot(application)))
    else:
        logging.warning("TELEGRAM_BOT_TOKEN not set — running dashboard only.")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
