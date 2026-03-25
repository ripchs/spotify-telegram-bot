"""
storage.py — работа с users.json.
Каждый пользователь хранится по ключу str(telegram_user_id).
"""

import json
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")

# Структура одного пользователя по умолчанию
DEFAULT_USER: dict = {
    # Spotify
    "spotify_client_id":     None,
    "spotify_client_secret": None,
    # Telegram
    "telegram_channel_id":   None,
    "message_id":            None,
    # Настройки
    "poll_active":           10,
    "poll_idle":             30,
    "max_history":           6,
    # Состояние
    "history":               [],
    "stats":                 {},
    "setup_step":            None,
    "active":                False,
}


def token_cache_path(uid: int) -> str:
    """Путь к файлу кэша токена Spotify для конкретного пользователя."""
    return os.path.join(BASE_DIR, f"token_cache_{uid}")


def _load() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Не удалось загрузить users.json: {e}")
    return {}


def _save(data: dict) -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Не удалось сохранить users.json: {e}")


def get_user(uid: int) -> Optional[dict]:
    return _load().get(str(uid))


def get_all_users() -> dict:
    return _load()


def set_user(uid: int, data: dict) -> None:
    all_users = _load()
    all_users[str(uid)] = data
    _save(all_users)


def update_user(uid: int, **kwargs) -> None:
    all_users = _load()
    user = all_users.get(str(uid), dict(DEFAULT_USER))
    user.update(kwargs)
    all_users[str(uid)] = user
    _save(all_users)


def create_user(uid: int) -> dict:
    user = dict(DEFAULT_USER)
    set_user(uid, user)
    return user


def delete_user(uid: int) -> None:
    all_users = _load()
    all_users.pop(str(uid), None)
    _save(all_users)
    # Удаляем кэш токена если есть
    cache = token_cache_path(uid)
    if os.path.exists(cache):
        os.remove(cache)


def is_setup_complete(user: dict) -> bool:
    return all([
        user.get("spotify_client_id"),
        user.get("spotify_client_secret"),
        user.get("telegram_channel_id"),
        user.get("message_id"),
        user.get("setup_step") is None,
    ])
