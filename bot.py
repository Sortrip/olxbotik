import json, os, re
import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
URL = os.environ["OLX_URL"]
SEEN_FILE = "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "uk,ru;q=0.9"}

def send(text):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": text}, timeout=20)

def load_seen():
    try:
        return json.load(open(SEEN_FILE))
    except Exception:
        return None  # первый запуск

def fetch_ads():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    ads = {}
    for a in soup.select('a[href*="/obyavlenie/"]'):
        href = a["href"].split("#")[0].split("?")[0]
        if not href.startswith("http"):
            href = "https://www.olx.ua" + href
        m = re.search(r"-(ID\w+)\.html", href)
        ad_id = m.group(1) if m else href
        title = a.get_text(" ", strip=True)[:200] or "Объявление"
        ads.setdefault(ad_id, (title, href))
    return ads

def main():
    seen = load_seen()
    try:
        ads = fetch_ads()
    except Exception as e:
        print("fetch error:", e)
        if seen is None:
            json.dump([], open(SEEN_FILE, "w"))
        return
    if seen is None:
        json.dump(list(ads), open(SEEN_FILE, "w"))
        send(f"Бот запущен, запомнил {len(ads)} объявлений.")
        return
    seen_set = set(seen)
    new = [(i, t, l) for i, (t, l) in ads.items() if i not in seen_set]
    for i, t, l in new[:20]:
        send(f"🆕 {t}\n{l}")
    if len(new) > 20:
        send(f"…и ещё {len(new) - 20} новых")
    updated = seen + [i for i, _, _ in new]
    json.dump(updated[-3000:], open(SEEN_FILE, "w"))

main()
