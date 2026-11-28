"""
OLX-монитор в Telegram: меню, несколько поисков, красивые карточки, премиум за Telegram Stars.
Запуск: python olx_bot.py   (бот должен работать постоянно)

Переменные окружения:
  TG_TOKEN       токен от BotFather (обязательно)
  ADMIN_ID       твой Telegram Id (для /grant, /refund и уведомлений об ошибках)
  PRICE_30 / PRICE_90 / PRICE_FOREVER   цены тарифов в звёздах (по умолчанию 50 / 130 / 1000)
  SUPPORT        контакт для вопросов по оплате, например @username
  PROXY          необязательно, прокси для запросов к OLX, например http://user:pass@host:port
  RUN_SECONDS    для GitHub Actions: через сколько секунд бот сам остановится (0 = работать вечно)
  FULL_CARDS_FREE  1 = полные карточки (фото, описание, продавец) у всех, 0 = только у премиума
  MAX_AGE_MINUTES  присылать только объявления не старше N минут (по умолчанию 10, 0 = выкл.)
  MAX_AGE_DAYS   0 = присылать только сегодняшние объявления, 1 = ещё и вчерашние (если MAX_AGE_MINUTES=0)
  STRICT_DATE    1 = если возраст объявления определить нельзя, НЕ присылать (по умолчанию 1)
  SKIP_REFRESHED 1 = не присылать поднятые/обновлённые старые объявления («Оновлено ...»), по умолчанию 1
  TRIAL_DAYS     сколько дней премиума дарим новому пользователю (по умолчанию 2, 0 = выкл.)
  FEED_INTERVAL  как часто (сек) опрашивать общую ленту OLX для поиска по словам (по умолчанию 3)
  FREE_DELAY     задержка (сек) для бесплатных в поиске по словам (по умолчанию 180)
  NEW_WINDOW_MIN объявление считается «новым», если создано не раньше N минут назад (по умолчанию 60)
Админ-команды: /ad ссылка, /grant id дни|forever, /refund id charge_id, /stats
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, LabeledPrice, LinkPreviewOptions, Message, PreCheckoutQuery)
from bs4 import BeautifulSoup

TOKEN = os.environ["TG_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SUPPORT = os.environ.get("SUPPORT", "@admin")
PROXY = os.environ.get("PROXY") or None
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "0"))
FULL_CARDS_FREE = os.environ.get("FULL_CARDS_FREE", "1") == "1"  # 0 = полные карточки только у премиума
MAX_PHOTOS = 4
VERSION = "v9 (лента JSON API, поиск по словам, точный возраст)"
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", "10"))  # объявления старше этого не присылаем (0 = выкл.)
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0"))  # 0 = только сегодняшние, 1 = сегодня и вчера
STRICT_DATE = os.environ.get("STRICT_DATE", "1") == "1"  # возраст неизвестен -> не присылаем
SKIP_REFRESHED = os.environ.get("SKIP_REFRESHED", "1") == "1"  # «Оновлено» = старое объявление

FOREVER_DAYS = 365 * 50                      # «навсегда» = 50 лет
FOREVER_CUTOFF = 20 * 365 * 86400            # всё, что дальше 20 лет, показываем как «навсегда»
REF_BONUS_DAYS = 1                           # пригласившему, когда друг добавил первый поиск
REF_INVITED_BONUS_DAYS = 1                   # приглашённому сразу (сверху к триалу)
MILESTONE_EVERY, MILESTONE_BONUS_DAYS = 5, 7  # каждый 5-й друг = ещё +7 дней
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "2"))  # подарок новичку
FEED_INTERVAL = float(os.environ.get("FEED_INTERVAL", "3"))  # опрос общей ленты, сек
FEED_LIMIT, FEED_PAGES = 40, 3                      # объявлений на страницу / макс. страниц за опрос
FREE_DELAY = int(os.environ.get("FREE_DELAY", "180"))  # задержка бесплатным в поиске по словам, сек
NEW_WINDOW_MIN = int(os.environ.get("NEW_WINDOW_MIN", "60"))  # «новое» = создано не раньше N мин назад
MODERATION_LAG_MIN = 5                              # создание -> появление в ленте (по данным OLX ~4,4 мин)
SELLER_NEW_DAYS = 30                                # аккаунт моложе N дней = «свежий продавец»
# Тарифы. Хочешь добавить ещё — просто допиши строку (ключ без двоеточий).
PLANS = {
    "30": {"days": 30, "stars": int(os.environ.get("PRICE_30", "50")), "label": "30 дней"},
    "90": {"days": 90, "stars": int(os.environ.get("PRICE_90", "130")), "label": "90 дней"},
    "forever": {"days": FOREVER_DAYS, "stars": int(os.environ.get("PRICE_FOREVER", "1000")), "label": "Навсегда ♾"},
}
FREE_LIMIT, PREMIUM_LIMIT = 1, 10            # сколько поисков можно держать
FREE_INTERVAL, PREMIUM_INTERVAL = 300, 60    # как часто проверять (сек)
TICK = 15                                    # как часто цикл смотрит, кого пора проверить
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "uk,ru;q=0.9"}

db = sqlite3.connect("olx.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, premium_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    url TEXT, active INTEGER DEFAULT 1, last_check REAL DEFAULT 0, initialized INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS seen(search_id INTEGER, ad_id TEXT, PRIMARY KEY(search_id, ad_id));
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS referrals(invited_id INTEGER PRIMARY KEY, referrer_id INTEGER, ts REAL);
""")


def add_column(table, ddl):
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        db.commit()
        return True
    except sqlite3.OperationalError:  # колонка уже есть
        return False


add_column("users", "lang TEXT DEFAULT ''")
add_column("users", "warn_for REAL DEFAULT 0")
if add_column("users", "exp_for REAL DEFAULT 0"):  # первый запуск этой версии: по давно истёкшим не шлём уведомления
    db.execute("UPDATE users SET exp_for=premium_until WHERE premium_until>0 AND premium_until<?", (time.time(),))
    db.commit()
add_column("referrals", "rewarded INTEGER DEFAULT 1")  # старые рефералы уже награждены
add_column("searches", "kind TEXT DEFAULT 'url'")      # url — по ссылке, kw — по словам
add_column("searches", "q TEXT DEFAULT ''")
add_column("searches", "pmin INTEGER DEFAULT 0")
add_column("searches", "pmax INTEGER DEFAULT 0")
router = Router()



# ---------- тексты ----------
TXT = {
    "ru": {
        "start": "Привет! Я слежу за новыми объявлениями на OLX и присылаю их сразу после публикации.\n\n"
                 "Напиши, что искать, например: <code>iPhone 8 700-1600</code> (слова + цена от-до в грн). "
                 "Лишнее убирай минусом: <code>-plus</code>.\n\n"
                 "Или пришли ссылку на поиск с olx.ua — возьму все фильтры оттуда.",
        "gift": "🎁 <b>Подарок: {days} дн. премиума</b> — до {pl} поисков, проверка каждую минуту, карточки без подписи бота. "
                "Премиум активен {until}.",
        "add_help": "Напиши, что искать: <code>iPhone 8 700-1600</code> (слова + цена от-до в грн, минус-слова: <code>-plus</code>).\n"
                    "Или открой olx.ua, выставь фильтры и пришли ссылку из адресной строки.",
        "started_kw": "✅ Поиск #{id} запущен: «{q}»{price}\nПришлю новые объявления сразу после публикации.",
        "new_seller": "Свежий аккаунт продавца — осторожнее с предоплатой",
        "ago_sec": "⏱ Опубликовано {n} сек. назад", "ago_min": "⏱ Опубликовано {n} мин. назад",
        "limit": "Лимит поисков: {limit}. Удали лишний в «Мои поиски» или возьми премиум 👇",
        "added": "Добавил поиск #{id}. Через несколько секунд подтвержу запуск.",
        "started": "✅ Поиск #{id} запущен. Запомнил {n} текущих объявлений, дальше пришлю только новые.",
        "no_searches": "Поисков пока нет. Пришли ссылку с OLX.",
        "btn_add": "➕ Добавить поиск", "btn_list": "📋 Мои поиски", "btn_premium": "⭐ Премиум",
        "btn_ref": "👥 Пригласить друга", "btn_lang": "🌐 Мова / Язык",
        "btn_pause": "⏸ Пауза #{id}", "btn_resume": "▶️ Включить #{id}", "btn_del": "🗑 Удалить #{id}",
        "done": "Готово", "deleted": "Удалил",
        "active_limit": "Лимит активных поисков: {limit}. Поставь другой на паузу или возьми премиум.",
        "prem_active": "Премиум активен {until}.", "prem_free": "Сейчас у тебя бесплатный тариф.",
        "prem_body": "<b>Бесплатно:</b> {fl} поиск, новые объявления с задержкой до {fd} мин.\n"
                     "<b>Премиум:</b> до {pl} поисков, объявления приходят сразу, через секунды после публикации ⚡, карточки без подписи бота.\n"
                     "<b>Навсегда:</b> всё то же + все будущие обновления без доплат.",
        "btn_ref_free": "👥 Бесплатно за друзей",
        "plan_30": "30 дней", "plan_90": "90 дней", "plan_forever": "Навсегда ♾",
        "ref_text": "👥 <b>Приглашай друзей — получай премиум</b>\n\n"
                    "Когда друг запустит бота по твоей ссылке и добавит первый поиск, ты получишь "
                    "<b>+{bonus} дн. премиума</b>, а друг — +{inv} дн. в подарок.\n"
                    "🏆 Каждый {every}-й друг — ещё <b>+{mb} дн.</b> сверху.\n\n"
                    "Твоя ссылка:\n<code>{link}</code>\n\n"
                    "Засчитано приглашений: {n}\nЖдут первого поиска: {p}\n"
                    "Получено: {earned} дн.\nДо следующего бонуса: {left}",
        "btn_share": "📤 Поделиться ссылкой",
        "share_text": "Бот, который мгновенно присылает новые объявления с OLX",
        "ref_reward": "🎉 Твой друг добавил первый поиск — тебе <b>+{days} дн.</b> премиума! Премиум активен {until}.",
        "ref_milestone": "🏆 Ты пригласил уже {n} друзей — бонус <b>+{days} дн.</b> сверху!",
        "inv_title": "Премиум: {plan}", "inv_desc": "До {pl} поисков и проверка каждую минуту", "inv_label": "Премиум",
        "paid": "⭐ Спасибо! Премиум ({plan}) активен {until}.",
        "paysupport": "По вопросам оплаты и возвратов пиши {support}.",
        "plan_missing": "Тариф не найден, открой меню и попробуй снова.",
        "price_changed": "Цена изменилась, открой меню и выбери тариф заново.",
        "plan_alert": "Тариф не найден",
        "expiring": "⏳ Твой премиум закончится в течение суток. Продли, чтобы не терять быстрые проверки и лишние поиски 👇",
        "expired": "⌛ Премиум закончился. Теперь проверка раз в {fi} мин., активен {fl} поиск.{paused}\n"
                   "Продли, чтобы снова получать объявления за минуту 👇",
        "paused_note": "\nПоисков поставлено на паузу: {n}.",
        "teaser_age": "⏱ Ты увидел это через {n} мин. после публикации.\n⚡ В премиуме — сразу, без задержки.",
        "teaser": "⚡ В премиуме — сразу, без задержки, до {pl} поисков.",
        "btn_speedup": "⚡ Получать быстрее",
        "open_olx": "Открыть на OLX →", "since": "на OLX с {d}",
        "lang_set": "Язык: русский 🇷🇺",
        "forever": "навсегда",
    },
    "ua": {
        "start": "Привіт! Я стежу за новими оголошеннями на OLX і надсилаю їх одразу після публікації.\n\n"
                 "Напиши, що шукати, наприклад: <code>iPhone 8 700-1600</code> (слова + ціна від-до в грн). "
                 "Зайве прибирай мінусом: <code>-plus</code>.\n\n"
                 "Або надішли посилання на пошук з olx.ua — візьму всі фільтри звідти.",
        "gift": "🎁 <b>Подарунок: {days} дн. преміуму</b> — до {pl} пошуків, перевірка щохвилини, картки без підпису бота. "
                "Преміум активний {until}.",
        "add_help": "Напиши, що шукати: <code>iPhone 8 700-1600</code> (слова + ціна від-до в грн, мінус-слова: <code>-plus</code>).\n"
                    "Або відкрий olx.ua, постав фільтри й надішли посилання з адресного рядка.",
        "started_kw": "✅ Пошук #{id} запущено: «{q}»{price}\nНадішлю нові оголошення одразу після публікації.",
        "new_seller": "Свіжий акаунт продавця — обережніше з передоплатою",
        "ago_sec": "⏱ Опубліковано {n} с тому", "ago_min": "⏱ Опубліковано {n} хв тому",
        "limit": "Ліміт пошуків: {limit}. Видали зайвий у «Мої пошуки» або візьми преміум 👇",
        "added": "Додав пошук #{id}. За кілька секунд підтверджу запуск.",
        "started": "✅ Пошук #{id} запущено. Запам'ятав {n} поточних оголошень, далі надішлю лише нові.",
        "no_searches": "Пошуків поки немає. Надішли посилання з OLX.",
        "btn_add": "➕ Додати пошук", "btn_list": "📋 Мої пошуки", "btn_premium": "⭐ Преміум",
        "btn_ref": "👥 Запросити друга", "btn_lang": "🌐 Мова / Язык",
        "btn_pause": "⏸ Пауза #{id}", "btn_resume": "▶️ Увімкнути #{id}", "btn_del": "🗑 Видалити #{id}",
        "done": "Готово", "deleted": "Видалив",
        "active_limit": "Ліміт активних пошуків: {limit}. Постав інший на паузу або візьми преміум.",
        "prem_active": "Преміум активний {until}.", "prem_free": "Зараз у тебе безкоштовний тариф.",
        "prem_body": "<b>Безкоштовно:</b> {fl} пошук, нові оголошення із затримкою до {fd} хв.\n"
                     "<b>Преміум:</b> до {pl} пошуків, оголошення приходять одразу, за секунди після публікації ⚡, картки без підпису бота.\n"
                     "<b>Назавжди:</b> те саме + усі майбутні оновлення без доплат.",
        "btn_ref_free": "👥 Безкоштовно за друзів",
        "plan_30": "30 днів", "plan_90": "90 днів", "plan_forever": "Назавжди ♾",
        "ref_text": "👥 <b>Запрошуй друзів — отримуй преміум</b>\n\n"
                    "Коли друг запустить бота за твоїм посиланням і додасть перший пошук, ти отримаєш "
                    "<b>+{bonus} дн. преміуму</b>, а друг — +{inv} дн. у подарунок.\n"
                    "🏆 Кожен {every}-й друг — ще <b>+{mb} дн.</b> зверху.\n\n"
                    "Твоє посилання:\n<code>{link}</code>\n\n"
                    "Зараховано запрошень: {n}\nЧекають на перший пошук: {p}\n"
                    "Отримано: {earned} дн.\nДо наступного бонусу: {left}",
        "btn_share": "📤 Поділитися посиланням",
        "share_text": "Бот, який миттєво надсилає нові оголошення з OLX",
        "ref_reward": "🎉 Твій друг додав перший пошук — тобі <b>+{days} дн.</b> преміуму! Преміум активний {until}.",
        "ref_milestone": "🏆 Ти вже запросив {n} друзів — бонус <b>+{days} дн.</b> зверху!",
        "inv_title": "Преміум: {plan}", "inv_desc": "До {pl} пошуків і перевірка щохвилини", "inv_label": "Преміум",
        "paid": "⭐ Дякую! Преміум ({plan}) активний {until}.",
        "paysupport": "З питань оплати й повернень пиши {support}.",
        "plan_missing": "Тариф не знайдено, відкрий меню й спробуй знову.",
        "price_changed": "Ціна змінилась, відкрий меню й обери тариф знову.",
        "plan_alert": "Тариф не знайдено",
        "expiring": "⏳ Твій преміум закінчиться протягом доби. Продовж, щоб не втратити швидкі перевірки та зайві пошуки 👇",
        "expired": "⌛ Преміум закінчився. Тепер перевірка раз на {fi} хв., активний {fl} пошук.{paused}\n"
                   "Продовж, щоб знову отримувати оголошення за хвилину 👇",
        "paused_note": "\nПошуків поставлено на паузу: {n}.",
        "teaser_age": "⏱ Ти побачив це через {n} хв. після публікації.\n⚡ У преміумі — одразу, без затримки.",
        "teaser": "⚡ У преміумі — одразу, без затримки, до {pl} пошуків.",
        "btn_speedup": "⚡ Отримувати швидше",
        "open_olx": "Відкрити на OLX →", "since": "на OLX з {d}",
        "lang_set": "Мова: українська 🇺🇦",
        "forever": "назавжди",
    },
}


# ---------- база ----------
def ensure_user(uid, lang_code=None):
    """True, если пользователь только что появился в базе. Язык берём из настроек Telegram."""
    lang = "ua" if (lang_code or "").lower().startswith("uk") else "ru"
    cur = db.execute("INSERT OR IGNORE INTO users(id, lang) VALUES(?,?)", (uid, lang))
    if cur.rowcount != 1 and lang_code:
        db.execute("UPDATE users SET lang=? WHERE id=? AND (lang IS NULL OR lang='')", (lang, uid))
    db.commit()
    return cur.rowcount == 1


def lang_of(uid):
    r = db.execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
    return r["lang"] if r and r["lang"] in TXT else "ru"


def tr(uid, key, **kw):
    return TXT[lang_of(uid)][key].format(**kw)


def add_premium(uid, days):
    until = max(time.time(), premium_until(uid)) + days * 86400
    db.execute("UPDATE users SET premium_until=? WHERE id=?", (until, uid))
    db.commit()
    return until


def premium_until(uid):
    r = db.execute("SELECT premium_until FROM users WHERE id=?", (uid,)).fetchone()
    return r["premium_until"] if r else 0


def is_premium(uid):
    return premium_until(uid) > time.time()


def fmt_date(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def fmt_until(ts, lang="ru"):
    return TXT[lang]["forever"] if ts - time.time() > FOREVER_CUTOFF else f"до {fmt_date(ts)}"


# ---------- свежесть объявлений ----------
MONTHS = {"січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6, "липня": 7,
          "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
          "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}


def now_kyiv():
    try:
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=2)))


TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
DATE_RE = re.compile(r"(\d{1,2})\s+([а-яіїєґ]+)\s+(\d{4})")
CARD_DATE_RE = re.compile(r"(?:(?:оновлено|обновлено)\s+)?(?:(?:сьогодні|сегодня|вчора|вчера)\D{0,6}\d{1,2}:\d{2}"
                          r"|\d{1,2}\s+[а-яіїєґ]+\s+\d{4})", re.I)
TODAY_WORDS, YESTERDAY_WORDS = ("сьогодні", "сегодня"), ("вчора", "вчера")
REFRESH_WORDS = ("оновлено", "обновлено")


def parse_ad_dt(text):
    """Текст с датой -> (datetime по Киеву, есть_ли_время) или None, если не поняли."""
    t = (text or "").lower().replace("\xa0", " ")
    now = now_kyiv()
    tm = TIME_RE.search(t)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    if hh > 23 or mm > 59:
        tm, hh, mm = None, 0, 0
    if any(w in t for w in TODAY_WORDS):
        day = now.date()
    elif any(w in t for w in YESTERDAY_WORDS):
        day = now.date() - timedelta(days=1)
    else:
        m = DATE_RE.search(t)
        if not m or m.group(2) not in MONTHS:
            return None
        try:
            day = date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=now.tzinfo), bool(tm)


def age_ok(dt):
    """Укладывается ли момент dt (aware) в разрешённый возраст."""
    now = now_kyiv()
    if MAX_AGE_MINUTES:
        return now - dt <= timedelta(minutes=MAX_AGE_MINUTES)
    return dt.date() >= now.date() - timedelta(days=MAX_AGE_DAYS)


def freshness(text):
    """True = свежее, False = старое/поднятое, None = по тексту не понять."""
    t = (text or "").lower()
    if SKIP_REFRESHED and any(w in t for w in REFRESH_WORDS):
        return False
    p = parse_ad_dt(t)
    if not p:
        return None
    dt, has_time = p
    if has_time or not MAX_AGE_MINUTES:
        return age_ok(dt)
    # дата без времени: если даже самый конец того дня уже слишком давно — старое, иначе не знаем
    if now_kyiv() - (dt + timedelta(hours=23, minutes=59)) > timedelta(minutes=MAX_AGE_MINUTES):
        return False
    return None


def parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    tz = now_kyiv().tzinfo
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def page_freshness(d):
    # Свежесть по данным со страницы. Объявление попадает в ленту через ~4-5 мин после создания (модерация),
    # поэтому возраст считаем от публикации (last_refresh), а «новое» определяем по времени создания.
    if not d:
        return None
    c, r = parse_iso(d.get("created")), parse_iso(d.get("refreshed"))
    if c or r:
        if c and now_kyiv() - c > timedelta(minutes=NEW_WINDOW_MIN):
            return False  # старое объявление (в т.ч. поднятое)
        return age_ok(r or (c + timedelta(minutes=MODERATION_LAG_MIN)))
    return freshness(d.get("date"))


# ---------- OLX ----------
def prepare_url(url):
    """Принудительно сортируем по новым."""
    u = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k != "search[order]"]
    q.append(("search[order]", "created_at:desc"))
    return urlunparse(u._replace(query=urlencode(q, safe="[]:")))


def ad_from_link(a):
    link = urljoin("https://www.olx.ua", a["href"].split("#")[0].split("?")[0])
    m = re.search(r"-(ID\w+)\.html", link)
    return (m.group(1) if m else link), link


async def fetch_ads(session, url):
    async with session.get(url, headers=HEADERS, proxy=PROXY,
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        page = await r.text()
    soup = BeautifulSoup(page, "html.parser")
    ads = {}
    for card in soup.select('[data-cy="l-card"]'):
        a = card.select_one('a[href*="/obyavlenie/"]')
        if not a:
            continue
        ad_id, link = ad_from_link(a)
        title = card.select_one("h4, h6")
        price = card.select_one('[data-testid="ad-price"]')
        loc = card.select_one('[data-testid="location-date"]')
        img = card.select_one("img")
        src = (img.get("src") or "") if img else ""
        loc_txt = loc.get_text(" ", strip=True) if loc else ""
        date_txt = loc_txt
        if parse_ad_dt(loc_txt) is None:  # блок location-date пропал/изменился — ищем дату по всей карточке
            mm = CARD_DATE_RE.search(card.get_text(" ", strip=True))
            if mm:
                date_txt = mm.group(0)
        ads[ad_id] = {
            "link": link,
            "title": (title.get_text(" ", strip=True) if title else "Объявление")[:200],
            "price": price.get_text(" ", strip=True) if price else "",
            "loc": loc_txt,
            "date": date_txt,
            "img": src if src.startswith("http") else "",
        }
    if not ads:  # запасной вариант, если вёрстка карточек поменялась
        for a in soup.select('a[href*="/obyavlenie/"]'):
            ad_id, link = ad_from_link(a)
            ads.setdefault(ad_id, {"link": link, "title": a.get_text(" ", strip=True)[:200] or "Объявление",
                                   "price": "", "loc": "", "date": "", "img": ""})
    return ads


# ---------- отправка ----------
def deactivate_user(uid):
    db.execute("UPDATE searches SET active=0 WHERE user_id=?", (uid,))
    db.commit()


async def safe_send(bot, uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
    except TelegramForbiddenError:
        deactivate_user(uid)
    except Exception as e:
        logging.warning("send to %s failed: %s", uid, e)


# ---------- подробности объявления ----------
detail_cache = {}


def fix_photo(u):
    if isinstance(u, dict):
        u = u.get("link") or u.get("url")
    if not isinstance(u, str):
        return ""
    u = u.replace("{width}", "800").replace("{height}", "600")
    return u if u.startswith("http") else ""


def html_to_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return BeautifulSoup(s, "html.parser").get_text().strip()


def since_text(s):
    m = re.match(r"(\d{4})-(\d{2})", s or "")
    return f"{m.group(2)}.{m.group(1)}" if m else (s or "")


def parse_ad_page(page):
    """Достаём из страницы объявления описание, фото, продавца, параметры. Несколько способов по очереди."""
    d = {"title": "", "price": "", "desc": "", "photos": [], "seller": "", "since": "",
         "loc": "", "date": "", "created": "", "refreshed": "", "params": []}
    soup = BeautifulSoup(page, "html.parser")

    def meta(prop):
        t = soup.find("meta", attrs={"property": prop})
        return (t.get("content") or "").strip() if t else ""

    def txt(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else ""

    # 1) данные, которые OLX вшивает в саму страницу
    m = re.search(r'__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]+|\\.)*")', page)
    if m:
        try:
            ad = (json.loads(json.loads(m.group(1))).get("ad") or {}).get("ad") or {}
            d["title"] = ad.get("title") or ""
            d["desc"] = html_to_text(ad.get("description"))
            d["photos"] = [fix_photo(p) for p in ad.get("photos") or []]
            user = ad.get("user") or {}
            d["seller"] = user.get("name") or ""
            d["since"] = since_text(str(user.get("created") or ""))
            for k in ("createdTime", "created_time", "createdAt"):
                if ad.get(k):
                    d["created"] = str(ad[k])
                    break
            for k in ("lastRefreshTime", "last_refresh_time"):
                if ad.get(k):
                    d["refreshed"] = str(ad[k])
                    break
            for p in ad.get("params") or []:
                if isinstance(p, dict) and p.get("key") != "price" and p.get("name") and p.get("value"):
                    d["params"].append(f"{p['name']}: {p['value']}")
        except Exception as e:
            logging.info("prerendered state not parsed: %s", e)

    # 2) структурированные данные schema.org
    for sc in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(sc.string or "")
        except Exception:
            continue
        for it in (data if isinstance(data, list) else [data]):
            if isinstance(it, dict) and it.get("@type") == "Product":
                d["title"] = d["title"] or it.get("name") or ""
                d["desc"] = d["desc"] or html_to_text(it.get("description"))
                img = it.get("image")
                if not d["photos"]:
                    d["photos"] = [fix_photo(x) for x in ([img] if isinstance(img, str) else (img or []))]
                off = it.get("offers")
                if isinstance(off, dict) and off.get("price") and not d["price"]:
                    d["price"] = f"{off['price']} {off.get('priceCurrency', '')}".strip()

    # 3) обычная вёрстка и мета-теги
    if not d["desc"]:
        el = soup.select_one('[data-cy="ad_description"]')
        if el:
            d["desc"] = re.sub(r"^(Опис|Описание)\s*", "", el.get_text("\n", strip=True))
    d["desc"] = d["desc"] or meta("og:description")
    d["title"] = d["title"] or meta("og:title")
    d["seller"] = d["seller"] or txt('[data-testid="user-profile-user-name"]')
    d["since"] = d["since"] or txt('[data-testid="member-since"]')
    d["price"] = d["price"] or txt('[data-testid="ad-price-container"]')
    d["date"] = txt('[data-cy="ad-posted-at"]')
    d["loc"] = txt('[data-testid="location-date"]')
    if not d["params"]:
        d["params"] = [p.get_text(" ", strip=True)
                       for p in soup.select('[data-testid="ad-parameters-container"] p')][:6]
    if not any(d["photos"]):
        d["photos"] = [meta("og:image")] + [(im.get("src") or im.get("data-src") or "")
                                           for im in soup.select('img[src*="olxcdn"], img[data-src*="olxcdn"]')
                                           if "/files/" in (im.get("src") or im.get("data-src") or "")]
    uniq, keys = [], set()
    for u in d["photos"]:
        k = u.split(";")[0]
        if u.startswith("http") and k not in keys:
            keys.add(k)
            uniq.append(u)
    d["photos"] = uniq[:MAX_PHOTOS]
    return d


async def get_details(session, ad):
    if ad["link"] in detail_cache:
        return detail_cache[ad["link"]]
    try:
        async with session.get(ad["link"], headers=HEADERS, proxy=PROXY,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            d = parse_ad_page(await r.text())
        if len(detail_cache) > 300:
            detail_cache.clear()
        detail_cache[ad["link"]] = d
    except Exception as e:
        logging.warning("details failed for %s: %s", ad["link"], e)
        d = {}
    await asyncio.sleep(1)
    return d


def build_caption(ad, d, lang="ru", foot_extra="", head_extra=""):
    t = TXT[lang]
    title = ad["title"] if ad["title"] != "Объявление" else (d.get("title") or "Объявление")
    price = ad["price"] or d.get("price", "")
    loc = ad["loc"] or d.get("loc", "")
    head = f"<b>{escape(title[:200])}</b>\n"
    if price:
        head += f"💰 {escape(price)}\n"
    if loc:
        head += f"📍 {escape(loc)}\n"
    if d.get("date") and not re.search(r"\d{4}|сьогодні|сегодня", loc.lower()):
        head += f"🕒 {escape(d['date'])}\n"
    if d.get("seller"):
        since = d.get("since") or ""
        if re.fullmatch(r"\d{2}\.\d{4}", since):
            since = t["since"].format(d=since)
        head += "👤 " + escape(d["seller"] + (f" · {since}" if since else "")) + "\n"
    if d.get("fresh_seller"):
        head += f"⚠️ {escape(t['new_seller'])}\n"
    if d.get("params"):
        head += "🔧 " + escape(" · ".join(d["params"][:5])) + "\n"
    head += head_extra
    foot = f'\n<a href="{ad["link"]}">{t["open_olx"]}</a>' + foot_extra
    budget = 1024 - len(head) - len(foot) - 30
    desc = re.sub(r"\n{3,}", "\n\n", d.get("desc", "")).strip()
    if desc and budget > 80:
        if len(desc) > budget:
            desc = desc[:budget - 1].rsplit(" ", 1)[0] + "…"
        head += "\n" + escape(desc) + "\n"
    return head + foot


def ad_age_minutes(ad, d):
    # Сколько минут назад объявление опубликовано (для подписи у бесплатных); None — если неизвестно.
    d = d or {}
    dt = parse_iso(d.get("published") or d.get("refreshed"))
    if not dt:
        c = parse_iso(d.get("created"))
        if c:
            dt = c + timedelta(minutes=MODERATION_LAG_MIN)
    if not dt:
        p = parse_ad_dt(ad.get("date"))
        if p and p[1]:
            dt = p[0]
    if not dt:
        return None
    n = int((now_kyiv() - dt).total_seconds() // 60)
    return n if 1 <= n <= 120 else None


def ago_text(lang, d):
    # Строка «опубликовано N сек. назад» — доказательство скорости для премиум-пользователей.
    pdt = parse_iso((d or {}).get("published"))
    if not pdt:
        return ""
    sec = max(0, int((now_kyiv() - pdt).total_seconds()))
    t = TXT[lang]
    return (t["ago_sec"].format(n=sec) if sec < 120 else t["ago_min"].format(n=sec // 60)) + "\n"


async def send_ad(bot, uid, ad, d=None):
    d = d or {}
    lang = lang_of(uid)
    t = TXT[lang]
    premium = is_premium(uid)
    foot_extra, head_extra, rows = "", "", [[InlineKeyboardButton(text=t["open_olx"], url=ad["link"])]]
    if premium:
        head_extra = ago_text(lang, d)
    else:
        # бесплатным: подсказка про скорость премиума + реф-ссылка в подписи (карточки пересылают → приток людей)
        me = await bot.me()
        age = ad_age_minutes(ad, d)
        hint = t["teaser_age"].format(n=age) if age else t["teaser"].format(pl=PREMIUM_LIMIT)
        foot_extra = f'\n\n{hint}\n🤖 <a href="https://t.me/{me.username}?start=ref_{uid}">@{me.username}</a>'
        rows.append([InlineKeyboardButton(text=t["btn_speedup"], callback_data="premium")])
    text = build_caption(ad, d, lang, foot_extra, head_extra)
    photos = d.get("photos") or ([ad["img"]] if ad["img"] else [])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        if len(photos) > 1:  # альбом: подпись на первой фото (кнопки у альбома не бывают)
            try:
                media = [InputMediaPhoto(media=u, caption=text if i == 0 else None, parse_mode="HTML")
                         for i, u in enumerate(photos)]
                await bot.send_media_group(uid, media)
                return
            except TelegramForbiddenError:
                raise
            except Exception as e:
                logging.info("album failed: %s", e)
        if photos:
            try:
                await bot.send_photo(uid, photos[0], caption=text, reply_markup=kb)
                return
            except TelegramForbiddenError:
                raise
            except Exception as e:
                logging.info("photo failed: %s", e)
        await bot.send_message(uid, text, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramForbiddenError:
        deactivate_user(uid)
    except Exception as e:
        logging.warning("send_ad to %s failed: %s", uid, e)


# ---------- фоновая проверка ----------
async def poller(bot):
    fails = 0
    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()
            due = [s for s in db.execute("SELECT * FROM searches WHERE active=1 AND kind='url'").fetchall()
                   if now - s["last_check"] >= (PREMIUM_INTERVAL if is_premium(s["user_id"]) else FREE_INTERVAL)]
            cache = {}  # одинаковые ссылки грузим один раз за круг
            for s in due:
                url = s["url"]
                if url not in cache:
                    try:
                        cache[url] = await fetch_ads(session, url)
                        fails = 0
                        await asyncio.sleep(2)
                    except Exception as e:
                        cache[url] = None
                        fails += 1
                        logging.warning("fetch failed (%s): %s", fails, e)
                        if fails == 10 and ADMIN_ID:
                            await safe_send(bot, ADMIN_ID, f"⚠️ OLX 10 раз подряд отдаёт ошибку: {escape(str(e))}")
                        await asyncio.sleep(min(30 * fails, 300))
                ads = cache[url]
                if not ads:
                    if ads is not None:
                        logging.warning("search %s: 0 карточек, вёрстка могла измениться", s["id"])
                    continue
                db.execute("UPDATE searches SET last_check=? WHERE id=?", (time.time(), s["id"]))
                seen = {r[0] for r in db.execute("SELECT ad_id FROM seen WHERE search_id=?", (s["id"],))}
                new = [(i, a) for i, a in ads.items() if i not in seen]
                db.executemany("INSERT OR IGNORE INTO seen VALUES(?,?)", [(s["id"], i) for i, _ in new])
                if not s["initialized"]:
                    db.execute("UPDATE searches SET initialized=1 WHERE id=?", (s["id"],))
                    db.commit()
                    await safe_send(bot, s["user_id"], tr(s["user_id"], "started", id=s["id"], n=len(ads)))
                    continue
                db.commit()
                full = FULL_CARDS_FREE or is_premium(s["user_id"])
                to_send = []
                for i, a in new[:30]:  # новые идут сверху
                    f = freshness(a["date"])
                    if f is False:
                        logging.info("пропущено старое: %s | %s", a["link"], a["date"])
                        continue
                    d = None
                    if f is None or full:
                        d = await get_details(session, a)
                        pf = page_freshness(d)
                        if pf is False:
                            logging.info("пропущено старое (страница): %s | %s | %s",
                                         a["link"], d.get("created"), d.get("date"))
                            continue
                        if pf is None and f is None and STRICT_DATE:
                            logging.info("пропущено, возраст неизвестен: %s | %r", a["link"], a["date"])
                            continue
                    to_send.append((a, d if full else None))
                    if len(to_send) >= 10:
                        break
                for a, d in reversed(to_send):  # старые первыми, новые последними
                    await send_ad(bot, s["user_id"], a, d)
                    await asyncio.sleep(0.5)
            await asyncio.sleep(TICK)


# ---------- общая лента OLX (JSON API) и поиск по словам ----------
FEED_URL = "https://www.olx.ua/api/v1/offers/"
API_HEADERS = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json", "Accept-Language": "uk,ru;q=0.9"}


def norm_title(s):
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def parse_query(q):
    # «iphone 8 -plus» -> нужные слова ['iphone','8'], исключения ['plus']
    pos, neg = [], []
    for raw in (q or "").split():
        if raw.startswith("-") and len(raw) > 1:
            neg += norm_title(raw[1:]).split()
        else:
            pos += norm_title(raw).split()
    return pos, neg


def title_has(title_n, tok):
    # слово должно НАЧИНАТЬСЯ с tok: «8» найдёт «iphone 8», но не «iphone 128gb»
    return re.search(r"(?<!\w)" + re.escape(tok), title_n) is not None


def ad_matches(s, ad, parsed=None):
    pos, neg = parsed or parse_query(s["q"])
    if not pos:
        return False
    t = ad["title_n"]
    if not all(title_has(t, w) for w in pos):
        return False
    if any(title_has(t, w) for w in neg):
        return False
    if s["pmin"] or s["pmax"]:
        p = ad.get("price_uah")
        if p is None:
            return False
        if s["pmin"] and p < s["pmin"]:
            return False
        if s["pmax"] and p > s["pmax"]:
            return False
    return True


def price_note(pmin, pmax):
    if pmin and pmax:
        return f" · {pmin}–{pmax} грн"
    if pmax:
        return f" · ≤ {pmax} грн"
    if pmin:
        return f" · ≥ {pmin} грн"
    return ""


def parse_kw(text):
    # «iphone 8 -plus 700-1600» / «ps5 до 15000» / «ps5 від 8000» -> (запрос, цена от, цена до)
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"\s*(грн\.?|uah|₴)\s*$", "", t, flags=re.I)
    pmin = pmax = 0
    m = re.search(r"(?:^|\s)(\d{2,7})\s*[-–—]\s*(\d{2,7})$", t)
    if m:
        pmin, pmax = sorted((int(m.group(1)), int(m.group(2))))
        t = t[:m.start()]
    else:
        m = re.search(r"(?:^|\s)(?:до|up to|<=?)\s*(\d{2,7})$", t, flags=re.I)
        if m:
            pmax = int(m.group(1))
            t = t[:m.start()]
        else:
            m = re.search(r"(?:^|\s)(?:від|от|from|>=?)\s*(\d{2,7})$", t, flags=re.I)
            if m:
                pmin = int(m.group(1))
                t = t[:m.start()]
    return t.strip()[:100], pmin, pmax


def normalize_offer(it):
    # Один объект из JSON-ленты OLX -> карточка для send_ad (+ подробности без похода на страницу).
    try:
        if it.get("status") not in (None, "active"):
            return None  # берём только опубликованные
        link = (it.get("url") or "").strip()
        if not link:
            return None
        title = (it.get("title") or "Объявление").strip()
        created = parse_iso(it.get("created_time"))
        refreshed = parse_iso(it.get("last_refresh_time")) or created
        price_label, price_uah, extra = "", None, []
        for p in it.get("params") or []:
            if not isinstance(p, dict):
                continue
            v = p.get("value") if isinstance(p.get("value"), dict) else {}
            if p.get("key") == "price":
                price_label = v.get("label") or ""
                val = v.get("converted_value") if v.get("converted_value") is not None else v.get("value")
                if isinstance(val, (int, float)):
                    price_uah = float(val)
                if price_label and v.get("negotiable"):
                    price_label += " (торг)"
            elif p.get("name") and v.get("label"):
                extra.append(f"{p['name']}: {v['label']}")
        loc = it.get("location") or {}
        city = (loc.get("city") or {}).get("name") or (loc.get("region") or {}).get("name") or ""
        district = (loc.get("district") or {}).get("name") or ""
        loc_str = ", ".join(x for x in (city, district) if x)
        user = it.get("user") or {}
        reg = parse_iso(user.get("created"))
        photos = []
        for ph in it.get("photos") or []:
            u = fix_photo(ph)
            if u:
                photos.append(u.replace(":443/", "/"))
            if len(photos) >= MAX_PHOTOS:
                break
        d = {
            "title": title, "price": price_label, "loc": loc_str, "date": "", "params": extra[:6],
            "desc": html_to_text(it.get("description") or ""), "photos": photos,
            "seller": user.get("name") or "",
            "since": f"{reg.month:02d}.{reg.year}" if reg else "",
            "fresh_seller": bool(reg and now_kyiv() - reg <= timedelta(days=SELLER_NEW_DAYS)),
            "created": created.isoformat() if created else "",
            "published": refreshed.isoformat() if refreshed else "",
        }
        return {"id": str(it["id"]), "link": link, "title": title[:200], "price": price_label, "loc": loc_str,
                "img": photos[0] if photos else "", "date": "", "price_uah": price_uah,
                "title_n": norm_title(title), "created_dt": created, "pub_dt": refreshed,
                "promoted": False, "d": d}
    except Exception as e:
        logging.info("normalize_offer: %s", e)
        return None


def feed_fresh(ad):
    # «Новое»: создано не раньше NEW_WINDOW_MIN мин назад (отсекает поднятые старые) И опубликовано не раньше лимита.
    c, p = ad["created_dt"], ad["pub_dt"]
    if not c or not p:
        return False
    if now_kyiv() - c > timedelta(minutes=NEW_WINDOW_MIN):
        return False
    return age_ok(p)


async def feed_page(session, offset=0):
    params = {"offset": offset, "limit": FEED_LIMIT, "sort_by": "created_at:desc"}
    async with session.get(FEED_URL, params=params, headers=API_HEADERS, proxy=PROXY,
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        data = await r.json(content_type=None)
    items = data.get("data") or []
    promoted = set((data.get("metadata") or {}).get("promoted") or [])
    ads = []
    for i, it in enumerate(items):
        ad = normalize_offer(it)
        if ad:
            ad["promoted"] = i in promoted
            ads.append(ad)
    return ads


async def fetch_feed(session):
    # Свежие объявления из общей ленты. Идём по страницам, пока на них есть недавно опубликованное.
    out, ids = [], set()
    for page in range(FEED_PAGES):
        ads = await feed_page(session, page * FEED_LIMIT)
        recent = False
        for ad in ads:
            if not ad["promoted"] and ad["pub_dt"] and age_ok(ad["pub_dt"]):
                recent = True
            if ad["id"] not in ids and feed_fresh(ad):
                ids.add(ad["id"])
                out.append(ad)
        if not recent:
            break
    return out


async def deliver_ad(bot, uid, sid, ad):
    row = db.execute("SELECT active FROM searches WHERE id=?", (sid,)).fetchone()
    if not row or not row["active"]:
        return
    cur = db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (sid, ad["id"]))
    db.commit()
    if cur.rowcount != 1:  # уже отправляли
        return
    full = FULL_CARDS_FREE or is_premium(uid)
    await send_ad(bot, uid, ad, ad["d"] if full else None)
    await asyncio.sleep(0.05)


async def init_kw_searches(bot):
    # Новый поиск по словам стартует «с этой секунды»: старое не присылаем.
    for s in db.execute("SELECT * FROM searches WHERE active=1 AND kind='kw' AND initialized=0").fetchall():
        db.execute("UPDATE searches SET initialized=1, last_check=? WHERE id=?", (time.time(), s["id"]))
        db.commit()
        await safe_send(bot, s["user_id"], tr(s["user_id"], "started_kw", id=s["id"], q=escape(s["q"]),
                                              price=price_note(s["pmin"], s["pmax"])))


async def dispatch_feed(bot, ads, pending):
    # Сопоставляем свежие объявления со всеми поисками по словам. Премиум — сразу, бесплатные — с задержкой.
    for s in db.execute("SELECT * FROM searches WHERE active=1 AND kind='kw' AND initialized=1").fetchall():
        uid, parsed = s["user_id"], parse_query(s["q"])
        for ad in ads:
            if not ad_matches(s, ad, parsed):
                continue
            key = (s["id"], ad["id"])
            if key in pending or db.execute("SELECT 1 FROM seen WHERE search_id=? AND ad_id=?", key).fetchone():
                continue
            if is_premium(uid):
                await deliver_ad(bot, uid, s["id"], ad)
            else:
                pending[key] = (time.time() + FREE_DELAY, uid, ad)


async def deliver_pending(bot, pending):
    now = time.time()
    for key in [k for k, v in pending.items() if v[0] <= now]:
        _, uid, ad = pending.pop(key)
        await deliver_ad(bot, uid, key[0], ad)


async def feed_poller(bot):
    done, pending, fails = {}, {}, 0
    async with aiohttp.ClientSession() as session:
        while True:
            t0 = time.time()
            try:
                await init_kw_searches(bot)
                ads = await fetch_feed(session)
                fails = 0
                fresh = [a for a in ads if a["id"] not in done]
                for a in fresh:
                    done[a["id"]] = t0
                if fresh:
                    await dispatch_feed(bot, fresh, pending)
                await deliver_pending(bot, pending)
                for k in [k for k, v in done.items() if t0 - v > 1800]:
                    del done[k]
            except Exception as e:
                fails += 1
                logging.warning("feed error (%s): %s", fails, e)
                if fails == 10 and ADMIN_ID:
                    await safe_send(bot, ADMIN_ID, f"⚠️ Лента OLX не отвечает ({e}). Поиск по словам не работает.")
                await asyncio.sleep(min(3 * fails, 60))
            await asyncio.sleep(max(0.5, FEED_INTERVAL - (time.time() - t0)))


# ---------- меню ----------
def main_kb(uid):
    t = TXT[lang_of(uid)]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t["btn_add"], callback_data="add")],
        [InlineKeyboardButton(text=t["btn_list"], callback_data="list")],
        [InlineKeyboardButton(text=t["btn_premium"], callback_data="premium")],
        [InlineKeyboardButton(text=t["btn_ref"], callback_data="ref")],
        [InlineKeyboardButton(text=t["btn_lang"], callback_data="lang")],
    ])


def plan_label(uid, key):
    return TXT[lang_of(uid)]["plan_" + key]


def plan_line(uid, key):
    p = PLANS[key]
    line = f"{plan_label(uid, key)} — {p['stars']} ⭐"
    if key != "forever" and p["days"] > 30:
        save = round(100 - 100 * p["stars"] / (PLANS["30"]["stars"] * p["days"] / 30))
        if save > 0:
            line += f" (−{save}%)"
    return line


def premium_kb(uid, with_ref=True):
    rows = [[InlineKeyboardButton(text=plan_line(uid, k), callback_data=f"buy:{k}")] for k in PLANS]
    if with_ref:
        rows.append([InlineKeyboardButton(text=tr(uid, "btn_ref_free"), callback_data="ref")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    uid = m.from_user.id
    is_new = ensure_user(uid, m.from_user.language_code)
    bonus = 0
    if is_new:
        bonus = TRIAL_DAYS
        if command.args and command.args.startswith("ref_") and register_referral(uid, command.args[4:]):
            bonus += REF_INVITED_BONUS_DAYS
    text = tr(uid, "start")
    if bonus:
        until = add_premium(uid, bonus)
        text = tr(uid, "gift", days=bonus, pl=PREMIUM_LIMIT, until=fmt_until(until, lang_of(uid))) + "\n\n" + text
    await m.answer(text, reply_markup=main_kb(uid))


@router.callback_query(F.data == "lang")
async def switch_lang(c: CallbackQuery):
    uid = c.from_user.id
    ensure_user(uid, c.from_user.language_code)
    new = "ua" if lang_of(uid) == "ru" else "ru"
    db.execute("UPDATE users SET lang=? WHERE id=?", (new, uid))
    db.commit()
    await c.message.answer(tr(uid, "lang_set") + "\n\n" + tr(uid, "start"), reply_markup=main_kb(uid))
    await c.answer()


@router.callback_query(F.data == "add")
async def add_help(c: CallbackQuery):
    await c.message.answer(tr(c.from_user.id, "add_help"))
    await c.answer()


@router.message(F.text.regexp(r"https?://\S*olx\.ua\S*"))
async def add_search(m: Message):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.language_code)
    limit = PREMIUM_LIMIT if is_premium(uid) else FREE_LIMIT
    count = db.execute("SELECT COUNT(*) FROM searches WHERE user_id=?", (uid,)).fetchone()[0]
    if count >= limit:
        await m.answer(tr(uid, "limit", limit=limit), reply_markup=premium_kb(uid))
        return
    url = prepare_url(re.search(r"https?://\S+", m.text).group(0))
    cur = db.execute("INSERT INTO searches(user_id, url) VALUES(?,?)", (uid, url))
    db.commit()
    await m.answer(tr(uid, "added", id=cur.lastrowid))
    await reward_referrer(m.bot, uid)  # друг добавил первый поиск -> бонус пригласившему


@router.message(F.text & ~F.text.startswith("/"))
async def add_keyword_search(m: Message):
    uid = m.from_user.id
    ensure_user(uid, m.from_user.language_code)
    text = (m.text or "").strip()
    if re.search(r"https?://", text):  # ссылка, но не с olx.ua
        return await m.answer(tr(uid, "add_help"))
    q, pmin, pmax = parse_kw(text)
    if len(re.sub(r"[\s\-]", "", q)) < 2 or not parse_query(q)[0]:
        return await m.answer(tr(uid, "add_help"))
    limit = PREMIUM_LIMIT if is_premium(uid) else FREE_LIMIT
    count = db.execute("SELECT COUNT(*) FROM searches WHERE user_id=?", (uid,)).fetchone()[0]
    if count >= limit:
        return await m.answer(tr(uid, "limit", limit=limit), reply_markup=premium_kb(uid))
    cur = db.execute("INSERT INTO searches(user_id, url, kind, q, pmin, pmax) VALUES(?,?,?,?,?,?)",
                     (uid, "kw:" + q, "kw", q, pmin, pmax))
    db.commit()
    await m.answer(tr(uid, "added", id=cur.lastrowid))
    await reward_referrer(m.bot, uid)


@router.callback_query(F.data == "list")
async def list_searches(c: CallbackQuery):
    uid = c.from_user.id
    t = TXT[lang_of(uid)]
    rows = db.execute("SELECT * FROM searches WHERE user_id=?", (uid,)).fetchall()
    if not rows:
        await c.message.answer(t["no_searches"], reply_markup=main_kb(uid))
        return await c.answer()
    lines, buttons = [], []
    for s in rows:
        if s["kind"] == "kw":
            path = s["q"][:45] + price_note(s["pmin"], s["pmax"])
        else:
            path = urlparse(s["url"]).path.strip("/")[:45] or "olx.ua"
        lines.append(f"#{s['id']} {'🟢' if s['active'] else '⏸'} {escape(path)}")
        buttons.append([InlineKeyboardButton(text=t["btn_pause" if s["active"] else "btn_resume"].format(id=s["id"]),
                                             callback_data=f"toggle:{s['id']}"),
                        InlineKeyboardButton(text=t["btn_del"].format(id=s["id"]), callback_data=f"del:{s['id']}")])
    await c.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await c.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def toggle(c: CallbackQuery):
    uid, sid = c.from_user.id, int(c.data.split(":")[1])
    row = db.execute("SELECT active FROM searches WHERE id=? AND user_id=?", (sid, uid)).fetchone()
    if not row:
        return await c.answer()
    if not row["active"]:  # включаем — проверяем лимит активных
        limit = PREMIUM_LIMIT if is_premium(uid) else FREE_LIMIT
        active = db.execute("SELECT COUNT(*) FROM searches WHERE user_id=? AND active=1", (uid,)).fetchone()[0]
        if active >= limit:
            return await c.answer(tr(uid, "active_limit", limit=limit), show_alert=True)
    db.execute("UPDATE searches SET active = 1 - active WHERE id=? AND user_id=?", (sid, uid))
    db.commit()
    await c.answer(tr(uid, "done"))


@router.callback_query(F.data.startswith("del:"))
async def delete(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    db.execute("DELETE FROM searches WHERE id=? AND user_id=?", (sid, c.from_user.id))
    db.execute("DELETE FROM seen WHERE search_id=?", (sid,))
    db.commit()
    await c.answer(tr(c.from_user.id, "deleted"))


# ---------- рефералка ----------
def register_referral(uid, arg):
    """Запоминаем, кто кого пригласил. Награда пригласившему — позже, когда друг добавит первый поиск."""
    try:
        ref = int(arg)
    except ValueError:
        return False
    if ref == uid or not db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
        return False
    cur = db.execute("INSERT OR IGNORE INTO referrals(invited_id, referrer_id, ts, rewarded) VALUES(?,?,?,0)",
                     (uid, ref, time.time()))
    db.commit()
    return cur.rowcount == 1


async def reward_referrer(bot, uid):
    r = db.execute("SELECT referrer_id FROM referrals WHERE invited_id=? AND rewarded=0", (uid,)).fetchone()
    if not r:
        return
    ref = r["referrer_id"]
    db.execute("UPDATE referrals SET rewarded=1 WHERE invited_id=?", (uid,))
    db.commit()
    until = add_premium(ref, REF_BONUS_DAYS)
    n = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded=1", (ref,)).fetchone()[0]
    extra = ""
    if MILESTONE_EVERY and n % MILESTONE_EVERY == 0:
        until = add_premium(ref, MILESTONE_BONUS_DAYS)
        extra = "\n" + tr(ref, "ref_milestone", n=n, days=MILESTONE_BONUS_DAYS)
    await safe_send(bot, ref, tr(ref, "ref_reward", days=REF_BONUS_DAYS, until=fmt_until(until, lang_of(ref))) + extra)


@router.callback_query(F.data == "ref")
async def ref_info(c: CallbackQuery):
    uid = c.from_user.id
    t = TXT[lang_of(uid)]
    me = await c.bot.me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    n = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded=1", (uid,)).fetchone()[0]
    pend = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded=0", (uid,)).fetchone()[0]
    earned = n * REF_BONUS_DAYS + (n // MILESTONE_EVERY) * MILESTONE_BONUS_DAYS
    share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote(t["share_text"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t["btn_share"], url=share)]])
    await c.message.answer(t["ref_text"].format(
        bonus=REF_BONUS_DAYS, inv=REF_INVITED_BONUS_DAYS, every=MILESTONE_EVERY, mb=MILESTONE_BONUS_DAYS,
        link=link, n=n, p=pend, earned=earned, left=MILESTONE_EVERY - n % MILESTONE_EVERY), reply_markup=kb)
    await c.answer()


# ---------- премиум за Stars ----------
@router.callback_query(F.data == "premium")
async def premium(c: CallbackQuery):
    uid = c.from_user.id
    t = TXT[lang_of(uid)]
    until = premium_until(uid)
    status = t["prem_active"].format(until=fmt_until(until, lang_of(uid))) if until > time.time() else t["prem_free"]
    body = t["prem_body"].format(fl=FREE_LIMIT, fd=max(FREE_DELAY, FREE_INTERVAL) // 60, pl=PREMIUM_LIMIT)
    await c.message.answer(f"{status}\n\n{body}", reply_markup=premium_kb(uid))
    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    uid = c.from_user.id
    key = c.data.split(":", 1)[1]
    plan = PLANS.get(key)
    if not plan:
        return await c.answer(tr(uid, "plan_alert"), show_alert=True)
    await c.message.answer_invoice(
        title=tr(uid, "inv_title", plan=plan_label(uid, key)),
        description=tr(uid, "inv_desc", pl=PREMIUM_LIMIT),
        payload=f"premium:{key}:{uid}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label=tr(uid, "inv_label"), amount=plan["stars"])])
    await c.answer()


def plan_from_payload(payload):
    """-> (ключ, тариф) или None."""
    parts = (payload or "").split(":")
    if parts[0] != "premium":
        return None
    if len(parts) == 2:  # старый формат счёта: premium:<uid> = 30 дней
        return "30", PLANS["30"]
    return (parts[1], PLANS[parts[1]]) if parts[1] in PLANS else None


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    found = plan_from_payload(q.invoice_payload)
    if not found:
        return await q.answer(ok=False, error_message=tr(q.from_user.id, "plan_missing"))
    legacy = q.invoice_payload.count(":") == 1
    if not legacy and q.total_amount != found[1]["stars"]:
        return await q.answer(ok=False, error_message=tr(q.from_user.id, "price_changed"))
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    p, uid = m.successful_payment, m.from_user.id
    ensure_user(uid, m.from_user.language_code)
    key, plan = plan_from_payload(p.invoice_payload) or ("30", PLANS["30"])
    cur = db.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?,?)",
                     (p.telegram_payment_charge_id, uid, p.total_amount, time.time()))
    if cur.rowcount != 1:  # этот платёж уже обработан
        db.commit()
        return
    until = add_premium(uid, plan["days"])
    await m.answer(tr(uid, "paid", plan=plan_label(uid, key), until=fmt_until(until, lang_of(uid))),
                   reply_markup=main_kb(uid))
    if ADMIN_ID:
        await safe_send(m.bot, ADMIN_ID, f"💰 Оплата {p.total_amount}⭐ ({plan['label']}) от {uid}\n"
                                         f"charge_id: <code>{escape(p.telegram_payment_charge_id)}</code>")


@router.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer(tr(m.from_user.id, "paysupport", support=escape(SUPPORT)))


# ---------- админские команды ----------
@router.message(Command("grant"))
async def grant(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        a = m.text.split()[1:3]
        uid = int(a[0])
        days = FOREVER_DAYS if a[1].lower() in ("forever", "навсегда") else int(a[1])
    except (ValueError, IndexError):
        return await m.answer("Формат: /grant user_id дни (или forever)")
    ensure_user(uid)
    until = max(time.time(), premium_until(uid)) + days * 86400
    db.execute("UPDATE users SET premium_until=? WHERE id=?", (until, uid))
    db.commit()
    await m.answer(f"Премиум для {uid}: {fmt_until(until)}")


@router.message(Command("refund"))
async def refund(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        _, uid, charge_id = m.text.split()
        await m.bot.refund_star_payment(user_id=int(uid), telegram_payment_charge_id=charge_id)
        db.execute("UPDATE users SET premium_until=0 WHERE id=?", (int(uid),))
        db.commit()
        await m.answer("Возврат сделан, премиум снят.")
    except Exception as e:
        await m.answer(f"Формат: /refund user_id charge_id\nОшибка: {escape(str(e))}")


@router.message(Command("feed"))
async def feed_debug(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    async with aiohttp.ClientSession() as session:
        try:
            ads = await feed_page(session, 0)
        except Exception as e:
            return await m.answer(f"Лента не открылась: {escape(str(e))}\nЕсли HTTP 403/429 — нужен прокси (PROXY) или другой сервер.")
    now = now_kyiv()
    fresh = [a for a in ads if feed_fresh(a)]

    def hm(dt):
        return dt.strftime("%H:%M:%S") if dt else "?"

    lines = [f"Лента ок: {len(ads)} объявл. на странице, свежих по правилам: {len(fresh)} "
             f"(возраст ≤ {MAX_AGE_MINUTES} мин., «новое» ≤ {NEW_WINDOW_MIN} мин.)"]
    for a in sorted(ads, key=lambda x: x["pub_dt"] or now, reverse=True)[:6]:
        lines.append(f"{'🟢' if feed_fresh(a) else '⚪'} {a['id']} создано {hm(a['created_dt'])} → в ленте {hm(a['pub_dt'])} "
                     f"| {escape(a['title'][:30])}")
    await m.answer("\n".join(lines))


@router.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    now = time.time()

    def q(sql, *a):
        return db.execute(sql, a).fetchone()[0]

    ua = q("SELECT COUNT(*) FROM users WHERE lang='ua'")
    await m.answer(
        f"👥 Пользователей: {q('SELECT COUNT(*) FROM users')} (UA: {ua})\n"
        f"⭐ С премиумом сейчас: {q('SELECT COUNT(*) FROM users WHERE premium_until>?', now)} "
        f"(навсегда: {q('SELECT COUNT(*) FROM users WHERE premium_until>?', now + FOREVER_CUTOFF)})\n"
        f"💳 Платили: {q('SELECT COUNT(DISTINCT user_id) FROM payments')} чел., "
        f"{q('SELECT COALESCE(SUM(stars),0) FROM payments')} ⭐\n"
        f"🔗 Рефералов засчитано: {q('SELECT COUNT(*) FROM referrals WHERE rewarded=1')}, "
        f"ждут первого поиска: {q('SELECT COUNT(*) FROM referrals WHERE rewarded=0')}\n"
        f"🔎 Активных поисков: {q('SELECT COUNT(*) FROM searches WHERE active=1')}")


@router.message(Command("ad"))
async def debug_ad(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Формат: /ad ссылка_на_объявление")
    async with aiohttp.ClientSession() as session:
        d = await get_details(session, {"link": parts[1]})
    if not d:
        return await m.answer("Не удалось загрузить страницу, смотри лог.")
    lines = [f"{k}: {len(v)} шт." if k == "photos" else f"{k}: {escape(str(v))[:150]}" for k, v in d.items()]
    verdict = {True: "свежее", False: "старое", None: "неизвестно"}[page_freshness(d)]
    lines.append(f"→ по странице: {verdict} (лимит {MAX_AGE_MINUTES} мин.)")
    await m.answer("\n".join(lines))
    await send_ad(m.bot, m.from_user.id, {"link": parts[1], "title": "Объявление", "price": "", "loc": "", "date": "", "img": ""}, d)


def enforce_limit(uid):
    """Бесплатному оставляем FREE_LIMIT самых старых активных поисков, остальные ставим на паузу."""
    rows = db.execute("SELECT id FROM searches WHERE user_id=? AND active=1 ORDER BY id", (uid,)).fetchall()
    extra = [r["id"] for r in rows[FREE_LIMIT:]]
    for sid in extra:
        db.execute("UPDATE searches SET active=0 WHERE id=?", (sid,))
    db.commit()
    return len(extra)


async def premium_watch(bot):
    """Предупреждаем за сутки до конца премиума и после окончания возвращаем лимиты бесплатного тарифа."""
    while True:
        try:
            now = time.time()
            for u in db.execute("SELECT id, premium_until FROM users WHERE premium_until>? AND premium_until<=? "
                                "AND warn_for!=premium_until", (now, now + 86400)).fetchall():
                db.execute("UPDATE users SET warn_for=? WHERE id=?", (u["premium_until"], u["id"]))
                db.commit()
                await safe_send(bot, u["id"], tr(u["id"], "expiring"), reply_markup=premium_kb(u["id"]))
            for u in db.execute("SELECT id, premium_until FROM users WHERE premium_until>0 AND premium_until<=? "
                                "AND exp_for!=premium_until", (now,)).fetchall():
                db.execute("UPDATE users SET exp_for=? WHERE id=?", (u["premium_until"], u["id"]))
                db.commit()
                n = enforce_limit(u["id"])
                note = tr(u["id"], "paused_note", n=n) if n else ""
                await safe_send(bot, u["id"], tr(u["id"], "expired", fi=FREE_INTERVAL // 60, fl=FREE_LIMIT, paused=note),
                                reply_markup=premium_kb(u["id"]))
            for r in db.execute("SELECT s.user_id FROM searches s JOIN users u ON u.id=s.user_id "
                                "WHERE s.active=1 AND u.premium_until<=? GROUP BY s.user_id HAVING COUNT(*)>?",
                                (now, FREE_LIMIT)).fetchall():
                enforce_limit(r["user_id"])
        except Exception:
            logging.exception("premium_watch")
        await asyncio.sleep(120)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    if ADMIN_ID:
        age = (f"не старше {MAX_AGE_MINUTES} мин." if MAX_AGE_MINUTES else
               "только сегодняшние" if MAX_AGE_DAYS == 0 else f"за последние {MAX_AGE_DAYS + 1} дн.")
        await safe_send(bot, ADMIN_ID, f"🟢 Бот запущен, версия {VERSION}\nПрисылаю: {age}",
                        disable_notification=True)
    task = asyncio.create_task(poller(bot))
    watch = asyncio.create_task(premium_watch(bot))
    feed = asyncio.create_task(feed_poller(bot))
    if RUN_SECONDS:
        async def stopper():
            await asyncio.sleep(RUN_SECONDS)
            await dp.stop_polling()
        asyncio.create_task(stopper())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        watch.cancel()
        feed.cancel()
        db.commit()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
