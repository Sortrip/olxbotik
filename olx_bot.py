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
  MAX_AGE_MINUTES  присылать только объявления не старше N минут (по умолчанию 60, 0 = выкл.)
  MAX_AGE_DAYS   0 = присылать только сегодняшние объявления, 1 = ещё и вчерашние (если MAX_AGE_MINUTES=0)
  STRICT_DATE    1 = если возраст объявления определить нельзя, НЕ присылать (по умолчанию 1)
  SKIP_REFRESHED 1 = не присылать поднятые/обновлённые старые объявления («Оновлено ...»), по умолчанию 1
Админ-команда /ad ссылка_на_объявление показывает, что бот смог вытащить из страницы.
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
VERSION = "v7 (строгий фильтр времени, тарифы, рефералка)"
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", "60"))  # объявления старше этого не присылаем (0 = выкл.)
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0"))  # 0 = только сегодняшние, 1 = сегодня и вчера
STRICT_DATE = os.environ.get("STRICT_DATE", "1") == "1"  # возраст неизвестен -> не присылаем
SKIP_REFRESHED = os.environ.get("SKIP_REFRESHED", "1") == "1"  # «Оновлено» = старое объявление

FOREVER_DAYS = 365 * 50                      # «навсегда» = 50 лет
FOREVER_CUTOFF = 20 * 365 * 86400            # всё, что дальше 20 лет, показываем как «навсегда»
REF_BONUS_DAYS = 1                           # дней премиума за каждого приглашённого
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
router = Router()


# ---------- база ----------
def ensure_user(uid):
    """True, если пользователь только что появился в базе."""
    cur = db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
    db.commit()
    return cur.rowcount == 1


def premium_until(uid):
    r = db.execute("SELECT premium_until FROM users WHERE id=?", (uid,)).fetchone()
    return r["premium_until"] if r else 0


def is_premium(uid):
    return premium_until(uid) > time.time()


def fmt_date(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def fmt_until(ts):
    return "навсегда" if ts - time.time() > FOREVER_CUTOFF else f"до {fmt_date(ts)}"


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
    """Свежесть по данным со страницы объявления (точное время создания приоритетнее текста)."""
    if not d:
        return None
    dt = parse_iso(d.get("created"))
    if dt:
        return age_ok(dt)
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
    return f"на OLX с {m.group(2)}.{m.group(1)}" if m else (s or "")


def parse_ad_page(page):
    """Достаём из страницы объявления описание, фото, продавца, параметры. Несколько способов по очереди."""
    d = {"title": "", "price": "", "desc": "", "photos": [], "seller": "", "since": "",
         "loc": "", "date": "", "created": "", "params": []}
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


def build_caption(ad, d):
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
        head += "👤 " + escape(d["seller"] + (f" · {d['since']}" if d.get("since") else "")) + "\n"
    if d.get("params"):
        head += "🔧 " + escape(" · ".join(d["params"][:5])) + "\n"
    foot = f'\n<a href="{ad["link"]}">Открыть на OLX →</a>'
    budget = 1024 - len(head) - len(foot) - 30
    desc = re.sub(r"\n{3,}", "\n\n", d.get("desc", "")).strip()
    if desc and budget > 80:
        if len(desc) > budget:
            desc = desc[:budget - 1].rsplit(" ", 1)[0] + "…"
        head += "\n" + escape(desc) + "\n"
    return head + foot


async def send_ad(bot, uid, ad, d=None):
    d = d or {}
    text = build_caption(ad, d)
    photos = d.get("photos") or ([ad["img"]] if ad["img"] else [])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть на OLX →", url=ad["link"])]])
    try:
        if len(photos) > 1:  # альбом: подпись на первой фото
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
            due = [s for s in db.execute("SELECT * FROM searches WHERE active=1").fetchall()
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
                    await safe_send(bot, s["user_id"],
                                    f"✅ Поиск #{s['id']} запущен. Запомнил {len(ads)} текущих объявлений, "
                                    f"дальше пришлю только новые.")
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


# ---------- меню ----------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить поиск", callback_data="add")],
        [InlineKeyboardButton(text="📋 Мои поиски", callback_data="list")],
        [InlineKeyboardButton(text="⭐ Премиум", callback_data="premium")],
        [InlineKeyboardButton(text=f"👥 Пригласить друга (+{REF_BONUS_DAYS} день)", callback_data="ref")],
    ])


@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    is_new = ensure_user(m.from_user.id)
    if is_new and command.args and command.args.startswith("ref_"):
        await handle_referral(m.bot, m.from_user.id, command.args[4:])
    await m.answer("Привет! Я слежу за новыми объявлениями на OLX и сразу присылаю их сюда.\n\n"
                   "Открой olx.ua, выставь нужные фильтры и пришли мне ссылку на результаты поиска.",
                   reply_markup=main_kb())


@router.callback_query(F.data == "add")
async def add_help(c: CallbackQuery):
    await c.message.answer("Открой olx.ua, выставь фильтры (категория, город, цена) и пришли мне ссылку из адресной строки.")
    await c.answer()


@router.message(F.text.regexp(r"https?://\S*olx\.ua\S*"))
async def add_search(m: Message):
    uid = m.from_user.id
    ensure_user(uid)
    limit = PREMIUM_LIMIT if is_premium(uid) else FREE_LIMIT
    count = db.execute("SELECT COUNT(*) FROM searches WHERE user_id=?", (uid,)).fetchone()[0]
    if count >= limit:
        await m.answer(f"Лимит поисков: {limit}. Удали лишний в «Мои поиски» или возьми премиум.",
                       reply_markup=main_kb())
        return
    url = prepare_url(re.search(r"https?://\S+", m.text).group(0))
    cur = db.execute("INSERT INTO searches(user_id, url) VALUES(?,?)", (uid, url))
    db.commit()
    await m.answer(f"Добавил поиск #{cur.lastrowid}. Через несколько секунд подтвержу запуск.")


@router.callback_query(F.data == "list")
async def list_searches(c: CallbackQuery):
    rows = db.execute("SELECT * FROM searches WHERE user_id=?", (c.from_user.id,)).fetchall()
    if not rows:
        await c.message.answer("Поисков пока нет. Пришли ссылку с OLX.", reply_markup=main_kb())
        return await c.answer()
    lines, buttons = [], []
    for s in rows:
        path = urlparse(s["url"]).path.strip("/")[:45] or "olx.ua"
        lines.append(f"#{s['id']} {'🟢' if s['active'] else '⏸'} {escape(path)}")
        buttons.append([InlineKeyboardButton(text=f"{'⏸ Пауза' if s['active'] else '▶️ Включить'} #{s['id']}",
                                             callback_data=f"toggle:{s['id']}"),
                        InlineKeyboardButton(text=f"🗑 Удалить #{s['id']}", callback_data=f"del:{s['id']}")])
    await c.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await c.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def toggle(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    db.execute("UPDATE searches SET active = 1 - active WHERE id=? AND user_id=?", (sid, c.from_user.id))
    db.commit()
    await c.answer("Готово")


@router.callback_query(F.data.startswith("del:"))
async def delete(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    db.execute("DELETE FROM searches WHERE id=? AND user_id=?", (sid, c.from_user.id))
    db.execute("DELETE FROM seen WHERE search_id=?", (sid,))
    db.commit()
    await c.answer("Удалил")


# ---------- рефералка ----------
async def handle_referral(bot, uid, arg):
    """Новый пользователь пришёл по ссылке ref_<id>: даём пригласившему бонус (один раз за человека)."""
    try:
        ref = int(arg)
    except ValueError:
        return
    if ref == uid or not db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
        return
    cur = db.execute("INSERT OR IGNORE INTO referrals(invited_id, referrer_id, ts) VALUES(?,?,?)",
                     (uid, ref, time.time()))
    if cur.rowcount != 1:
        db.commit()
        return
    until = max(time.time(), premium_until(ref)) + REF_BONUS_DAYS * 86400
    db.execute("UPDATE users SET premium_until=? WHERE id=?", (until, ref))
    db.commit()
    await safe_send(bot, ref, f"🎉 По твоей ссылке пришёл новый пользователь! +{REF_BONUS_DAYS} дн. премиума.\n"
                              f"Премиум активен {fmt_until(until)}.")


@router.callback_query(F.data == "ref")
async def ref_info(c: CallbackQuery):
    me = await c.bot.me()
    link = f"https://t.me/{me.username}?start=ref_{c.from_user.id}"
    n = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (c.from_user.id,)).fetchone()[0]
    share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote("Бот, который мгновенно присылает новые объявления с OLX")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share)]])
    await c.message.answer(
        f"👥 <b>Приглашай друзей — получай премиум</b>\n\n"
        f"За каждого нового пользователя, который запустит бота по твоей ссылке, "
        f"ты получаешь <b>+{REF_BONUS_DAYS} день премиума</b>.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {n}\nПолучено: {n * REF_BONUS_DAYS} дн.", reply_markup=kb)
    await c.answer()


# ---------- премиум за Stars ----------
def plan_line(key):
    p = PLANS[key]
    line = f"{p['label']} — {p['stars']} ⭐"
    if key != "forever" and p["days"] > 30:
        save = round(100 - 100 * p["stars"] / (PLANS["30"]["stars"] * p["days"] / 30))
        if save > 0:
            line += f" (−{save}%)"
    return line


@router.callback_query(F.data == "premium")
async def premium(c: CallbackQuery):
    until = premium_until(c.from_user.id)
    status = f"Премиум активен {fmt_until(until)}." if until > time.time() else "Сейчас у тебя бесплатный тариф."
    rows = [[InlineKeyboardButton(text=plan_line(k), callback_data=f"buy:{k}")] for k in PLANS]
    rows.append([InlineKeyboardButton(text=f"👥 Бесплатно за друзей (+{REF_BONUS_DAYS} день)", callback_data="ref")])
    await c.message.answer(
        f"{status}\n\n<b>Бесплатно:</b> {FREE_LIMIT} поиск, проверка раз в {FREE_INTERVAL // 60} мин.\n"
        f"<b>Премиум:</b> до {PREMIUM_LIMIT} поисков, проверка раз в {PREMIUM_INTERVAL} сек.\n"
        f"<b>Навсегда:</b> всё то же + все будущие обновления без доплат.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    key = c.data.split(":", 1)[1]
    plan = PLANS.get(key)
    if not plan:
        return await c.answer("Тариф не найден", show_alert=True)
    await c.message.answer_invoice(
        title=f"Премиум: {plan['label']}",
        description=f"До {PREMIUM_LIMIT} поисков и быстрая проверка новых объявлений",
        payload=f"premium:{key}:{c.from_user.id}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label="Премиум", amount=plan["stars"])])
    await c.answer()


def plan_from_payload(payload):
    parts = (payload or "").split(":")
    if parts[0] != "premium":
        return None
    if len(parts) == 2:  # старый формат счёта: premium:<uid> = 30 дней
        return PLANS["30"]
    return PLANS.get(parts[1])


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    plan = plan_from_payload(q.invoice_payload)
    if not plan:
        return await q.answer(ok=False, error_message="Тариф не найден, открой меню и попробуй снова.")
    legacy = q.invoice_payload.count(":") == 1
    if not legacy and q.total_amount != plan["stars"]:
        return await q.answer(ok=False, error_message="Цена изменилась, открой меню и выбери тариф заново.")
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    p, uid = m.successful_payment, m.from_user.id
    ensure_user(uid)
    plan = plan_from_payload(p.invoice_payload) or PLANS["30"]
    cur = db.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?,?)",
                     (p.telegram_payment_charge_id, uid, p.total_amount, time.time()))
    if cur.rowcount != 1:  # этот платёж уже обработан
        db.commit()
        return
    until = max(time.time(), premium_until(uid)) + plan["days"] * 86400
    db.execute("UPDATE users SET premium_until=? WHERE id=?", (until, uid))
    db.commit()
    await m.answer(f"⭐ Спасибо! Премиум ({plan['label']}) активен {fmt_until(until)}.", reply_markup=main_kb())
    if ADMIN_ID:
        await safe_send(m.bot, ADMIN_ID, f"💰 Оплата {p.total_amount}⭐ ({plan['label']}) от {uid}\n"
                                         f"charge_id: <code>{escape(p.telegram_payment_charge_id)}</code>")


@router.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer(f"По вопросам оплаты и возвратов пиши {escape(SUPPORT)}.")


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
    if RUN_SECONDS:
        async def stopper():
            await asyncio.sleep(RUN_SECONDS)
            await dp.stop_polling()
        asyncio.create_task(stopper())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        db.commit()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
