"""
main.py — точка входа.
Запускает Telegram Application + возобновляет активные трекеры.
Корректно завершается по Ctrl+C / SIGTERM.
"""

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_NAME     = os.getenv("OWNER_NAME", "владелец")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в .env!")


async def main() -> None:
    from bot import build_application, resume_active_trackers
    import storage

    app = build_application(TELEGRAM_TOKEN)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        log.info("🤖 Бот запущен. Ожидаю команды...")

        # Возобновляем трекеры тех, кто был активен до перезапуска
        await resume_active_trackers(app.bot)

        # Graceful shutdown по сигналу
        stop_event = asyncio.Event()

        def _signal_handler(*_):
            log.info("🛑 Получен сигнал остановки...")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _signal_handler)

        await stop_event.wait()

        # Останавливаем все трекеры перед выходом
        from bot import _tasks
        log.info(f"Останавливаю {len(_tasks)} трекер(ов)...")
        for uid, task in list(_tasks.items()):
            storage.update_user(uid, active=True)  # сохраняем active=True — при след. запуске возобновятся
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await app.updater.stop()
        await app.stop()

    log.info("✅ Бот остановлен корректно.")


if __name__ == "__main__":
    asyncio.run(main())
