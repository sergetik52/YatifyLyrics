# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys


APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR) if os.path.basename(APP_DIR).lower() == "dist" else APP_DIR


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_yes(prompt: str, default: bool = False) -> bool:
    default_text = "Д/н" if default else "д/Н"
    value = input(f"{prompt} [{default_text}]: ").strip().lower()
    if not value:
        return default
    return value in ("д", "да", "y", "yes", "1", "true")


def ask_choice(prompt: str, choices: dict[str, str], default: str) -> str:
    print(prompt)
    for key, text in choices.items():
        marker = " *" if key == default else ""
        print(f"  {key}. {text}{marker}")
    while True:
        value = input(f"Выбор [{default}]: ").strip() or default
        if value in choices:
            return value
        print("Введите один из вариантов выше.")


def load_existing_config() -> dict:
    path = os.path.join(ROOT_DIR, "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(config: dict):
    path = os.path.join(ROOT_DIR, "config.json")
    with open(path, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
    print(f"Конфиг сохранён: {path}")


def run_script(name: str):
    path = os.path.join(ROOT_DIR, name)
    if not os.path.exists(path):
        print(f"Файл не найден, пропускаю: {path}")
        return
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
        cwd=ROOT_DIR,
        check=False,
    )


def run_telegram_login():
    exe = os.path.join(ROOT_DIR, "dist", "TelegramLogin.exe")
    bat = os.path.join(ROOT_DIR, "telegram_login.bat")
    if os.path.exists(exe):
        subprocess.run([exe], cwd=ROOT_DIR, check=False)
    elif os.path.exists(bat):
        subprocess.run([bat], cwd=ROOT_DIR, check=False)
    else:
        print("TelegramLogin не найден.")


def default_product_mode(config: dict) -> str:
    discord_enabled = bool(config.get("discord_enabled", True))
    telegram_enabled = str(config.get("telegram_mode", "off")).lower() not in ("off", "none", "disabled", "false", "0")
    if discord_enabled and telegram_enabled:
        return "3"
    if telegram_enabled:
        return "2"
    return "1"


def configure_telegram(config: dict):
    mode = ask_choice(
        "Как подключать Telegram?",
        {
            "1": "через мой Telegram-аккаунт, можно прикреплять канал к профилю",
            "2": "через бота, проще, но без прикрепления к профилю",
        },
        "1" if str(config.get("telegram_mode", "user")).lower() != "bot" else "2",
    )
    config["telegram_mode"] = "user" if mode == "1" else "bot"

    config["telegram_channel"] = ask(
        "Канал Telegram, например @my_channel или -100...",
        str(config.get("telegram_channel") or config.get("telegram_chat_id") or ""),
    )
    config["telegram_chat_id"] = ask(
        "Канал для бота, можно тот же самый",
        str(config.get("telegram_chat_id") or config.get("telegram_channel") or ""),
    )
    config["telegram_bot_token"] = ask(
        "Токен бота от @BotFather, можно оставить пустым",
        str(config.get("telegram_bot_token") or ""),
    )

    if config["telegram_mode"] == "user":
        print("")
        print("Для режима аккаунта нужны api_id и api_hash с https://my.telegram.org")
        config["telegram_api_id"] = ask("Telegram api_id", str(config.get("telegram_api_id") or ""))
        config["telegram_api_hash"] = ask("Telegram api_hash", str(config.get("telegram_api_hash") or ""))
        config["telegram_phone"] = ask("Телефон Telegram, например +79990000000", str(config.get("telegram_phone") or ""))
        config["telegram_personal_channel"] = ask_yes(
            "Прикреплять канал к профилю, пока играет музыка?",
            bool(config.get("telegram_personal_channel", True)),
        )

    config["telegram_player_update_interval"] = float(
        ask("Как часто обновлять плеер в Telegram, секунд", str(config.get("telegram_player_update_interval") or "15.0"))
    )
    config["telegram_player_bar_width"] = int(ask("Длина полоски плеера", str(config.get("telegram_player_bar_width") or "10")))


def configure():
    config = load_existing_config()

    print("")
    print("Настройка Yatify")
    print("Эту программу можно запустить ещё раз, чтобы поменять настройки.")
    print("")

    product_mode = ask_choice(
        "Что включить?",
        {
            "1": "только Discord Activity",
            "2": "только Telegram-канал",
            "3": "Discord и Telegram вместе",
        },
        default_product_mode(config),
    )

    config["discord_enabled"] = product_mode in ("1", "3")
    telegram_enabled = product_mode in ("2", "3")

    if telegram_enabled:
        configure_telegram(config)
    else:
        config["telegram_mode"] = "off"

    print("")
    config["telegram_proxy"] = ask("Прокси для Telegram/HTTP, если нужен", str(config.get("telegram_proxy") or ""))
    config["lyrics_use_proxy"] = ask_yes("Использовать системный прокси для поиска текстов?", bool(config.get("lyrics_use_proxy", True)))
    config["cache_miss_ttl"] = float(
        ask("Сколько секунд помнить, что текст/обложка не найдены", str(config.get("cache_miss_ttl") or "86400.0"))
    )

    save_config(config)

    if telegram_enabled and config.get("telegram_mode") == "user" and ask_yes("Войти в Telegram сейчас?", True):
        run_telegram_login()

    if ask_yes("Добавить в автозапуск Windows и запустить сейчас?", True):
        run_script("install_autostart.ps1")

    print("")
    print("Готово. Логи будут тут: logs\\app.log")
    print("Чтобы поменять настройки, просто запусти этот мастер ещё раз.")


if __name__ == "__main__":
    try:
        configure()
    except KeyboardInterrupt:
        print("")
        print("Отменено.")
