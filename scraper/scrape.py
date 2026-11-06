"""
Обновление цен для сайта AppleBay.

Скрипт открывает страницы категорий, указанные в config.json (поле "source"),
собирает названия, фото, наличие и цены, применяет вашу наценку и сохраняет
результат в data/products.json. Сайт читает этот файл.

Запускается автоматически (см. .github/workflows/update-prices.yml),
но его можно запустить и вручную:  python scraper/scrape.py
"""

import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "data" / "products.json"

SOURCE_BASE = "https://gbstore.ru"
CATEGORY_URL = SOURCE_BASE + "/categories/{slug}"
MAX_PAGES = 20            # предохранитель от бесконечного листания
PAUSE_SECONDS = 2.0       # пауза между запросами, чтобы не нагружать чужой сайт
MIN_SHARE_OF_PREVIOUS = 0.5  # если товаров стало меньше половины — что-то сломалось

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u202f]{2,})\s*(?:руб|₽|р\.)", re.IGNORECASE)


def log(*args):
    print(*args, flush=True)


def norm_href(href: str) -> str:
    """Ссылка на товар без домена, параметров и хвостового слеша."""
    path = urlsplit(urljoin(SOURCE_BASE, href)).path
    return path.rstrip("/")


def parse_number(text: str):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def find_card(link):
    """Поднимаемся от ссылки на товар до блока карточки:
    самый большой родитель, в котором есть ссылки только на этот товар."""
    target = norm_href(link["href"])
    card = link
    node = link.parent
    for _ in range(12):
        if node is None or node.name in ("body", "html"):
            break
        hrefs = {norm_href(a["href"]) for a in node.select('a[href*="/products/"]')}
        if len(hrefs) > 1 or target not in hrefs:
            break
        card = node
        node = node.parent
    return card


def extract_price(card):
    # 1) типовые классы движка AdVantShop
    for sel in (".price-new .price-number", ".price-current .price-number",
                ".price-number", ".price-current", ".price-new", ".price"):
        el = card.select_one(sel)
        if el:
            n = parse_number(el.get_text(" "))
            if n and n >= 100:
                return n
    # 2) запасной вариант — все суммы «... руб.» в карточке, берём меньшую
    found = [parse_number(m.group(1)) for m in PRICE_RE.finditer(card.get_text(" "))]
    found = [n for n in found if n and n >= 100]
    return min(found) if found else None


def extract_image(card):
    img = card.find("img")
    if not img:
        return ""
    src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
    if not src or src.startswith("data:"):
        return ""
    return urljoin(SOURCE_BASE, src)


def extract_stock(card):
    text = card.get_text(" ").lower()
    if "нет в наличии" in text:
        return "out"
    if "под заказ" in text or "ожидается" in text:
        return "order"
    return "in"


def parse_page(html: str):
    soup = BeautifulSoup(html, "lxml")
    products = {}
    for link in soup.select('a[href*="/products/"]'):
        href = norm_href(link["href"])
        if href in products:
            continue
        card = find_card(link)
        price = extract_price(card)
        if not price:
            continue  # ссылка из меню или баннера, а не карточка
        junk = ("", "подробнее", "купить", "в корзину", "заказать")
        name = ""
        for a in card.select('a[href*="/products/"]'):
            candidate = (a.get("title") or a.get_text(" ")).strip()
            if candidate.lower() not in junk:
                name = candidate
                break
        if not name:
            img = card.find("img")
            name = (img.get("alt") or img.get("title") or "").strip() if img else ""
        if not name:
            continue
        products[href] = {
            "key": href.rsplit("/", 1)[-1],
            "name": re.sub(r"\s+", " ", name),
            "source_price": price,
            "image": extract_image(card),
            "stock": extract_stock(card),
        }
    return list(products.values())


def fetch(session, url):
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            log(f"   ! попытка {attempt + 1}: {e}")
            time.sleep(5 * (attempt + 1))
    return None


def scrape_category(session, slug):
    seen, items = set(), []
    for page in range(1, MAX_PAGES + 1):
        url = CATEGORY_URL.format(slug=slug) + (f"?page={page}" if page > 1 else "")
        html = fetch(session, url)
        time.sleep(PAUSE_SECONDS)
        if not html:
            break
        new = [p for p in parse_page(html) if p["key"] not in seen]
        if not new:
            break
        for p in new:
            seen.add(p["key"])
        items.extend(new)
    return items


def apply_markup(price, pricing):
    value = price * (1 + pricing.get("markup_percent", 0) / 100) + pricing.get("markup_rub", 0)
    step = max(int(pricing.get("round_to", 1) or 1), 1)
    return int(math.ceil(value / step) * step)


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    pricing = config.get("pricing", {})
    hide_out = pricing.get("hide_out_of_stock", True)

    previous_total = 0
    if OUTPUT_PATH.exists():
        try:
            previous_total = len(json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("products", []))
        except Exception:
            previous_total = 0

    session = requests.Session()
    result = []
    for group in config["groups"]:
        for cat in group["categories"]:
            log(f"→ {group['title']} / {cat['title']}  ({cat['source']})")
            items = scrape_category(session, cat["source"])
            kept = 0
            for p in items:
                if hide_out and p["stock"] == "out":
                    continue
                result.append({
                    "id": p["key"],
                    "name": p["name"],
                    "price": apply_markup(p["source_price"], pricing),
                    "image": p["image"],
                    "stock": p["stock"],
                    "category": cat["id"],
                    "group": group["id"],
                })
                kept += 1
            log(f"   найдено {len(items)}, на сайт {kept}")

    total = len(result)
    log(f"Итого товаров: {total} (было {previous_total})")

    if total == 0 or (previous_total and total < previous_total * MIN_SHARE_OF_PREVIOUS):
        log("Слишком мало товаров — возможно, сайт-источник изменился или недоступен. "
            "Старые цены оставлены без изменений.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "products": result,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    log("Готово: data/products.json обновлён")


if __name__ == "__main__":
    main()
