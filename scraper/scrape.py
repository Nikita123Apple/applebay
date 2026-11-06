"""
Обновление каталога и цен для сайта AppleBay.

1. Открывает главную страницу gbstore.ru и читает меню: какие есть разделы
   (Телефоны, Ноутбуки…) и какие подкатегории в каждом. Новые модели,
   которые появятся у них в меню, появятся и у вас — автоматически.
2. Обходит каждую подкатегорию, собирает названия, фото, наличие и цены.
3. Применяет вашу наценку из config.json и сохраняет всё в data/products.json.

Запускается автоматически (см. .github/workflows/update-prices.yml).
Вручную:  python scraper/scrape.py
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
MAX_PAGES = 30               # предохранитель от бесконечного листания
PAUSE_SECONDS = 1.5          # пауза между запросами, чтобы не нагружать чужой сайт
MIN_SHARE_OF_PREVIOUS = 0.5  # если товаров стало меньше половины — что-то сломалось

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u202f]{2,})\s*(?:руб|₽|р\.)", re.IGNORECASE)
JUNK_NAMES = ("", "подробнее", "купить", "в корзину", "заказать")


def log(*args):
    print(*args, flush=True)


def norm_href(href: str) -> str:
    """Ссылка без домена, параметров и хвостового слеша."""
    return urlsplit(urljoin(SOURCE_BASE, href)).path.rstrip("/")


def slug_of(href: str) -> str:
    return norm_href(href).rsplit("/", 1)[-1]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_number(text: str):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


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


# ---------- 1. Меню: разделы и подкатегории ----------

def discover_menu(session, groups):
    """Для каждого раздела из config.json находит в меню источника его подкатегории."""
    html = fetch(session, SOURCE_BASE + "/")
    if not html:
        log("! Не удалось открыть главную страницу источника")
        return {}
    soup = BeautifulSoup(html, "lxml")

    # «шапки» разделов: ссылка на категорию раздела с названием раздела
    def is_group_link(a, g):
        return slug_of(a["href"]) == g["source"] and clean(a.get_text()).lower() == g["title"].lower()

    result = {}
    for g in groups:
        tops = [a for a in soup.select('a[href*="/categories/"]') if is_group_link(a, g)]
        if not tops:
            log(f"  ! раздел «{g['title']}» не найден в меню")
            continue
        others = [o for o in groups if o is not g]
        best = None
        for li in tops[0].find_parents("li"):
            links = li.select('a[href*="/categories/"]')
            if any(is_group_link(a, o) for a in links for o in others):
                break  # поднялись слишком высоко — захватили соседний раздел
            best = li
        if best is None:
            continue
        other_sources = {o["source"] for o in others}
        subs, seen = [], {g["source"]}
        for a in best.select('a[href*="/categories/"]'):
            slug, title = slug_of(a["href"]), clean(a.get_text(" "))
            if slug in seen or slug in other_sources:
                continue
            if len(title) < 2 or title.endswith("!"):
                continue  # картинки и рекламные надписи «В наличии!»
            seen.add(slug)
            subs.append({"id": slug, "title": title, "source": slug})
        if subs:
            result[g["id"]] = subs
    return result


# ---------- 2. Товары на странице категории ----------

def find_card(link):
    """Самый большой родитель ссылки, где есть ссылки только на этот товар."""
    target = norm_href(link["href"])
    card, node = link, link.parent
    for _ in range(12):
        if node is None or node.name in ("body", "html"):
            break
        hrefs = {norm_href(a["href"]) for a in node.select('a[href*="/products/"]')}
        if len(hrefs) > 1 or target not in hrefs:
            break
        card, node = node, node.parent
    return card


def extract_price(card):
    for sel in (".price-new .price-number", ".price-current .price-number",
                ".price-number", ".price-current", ".price-new", ".price"):
        el = card.select_one(sel)
        if el:
            n = parse_number(el.get_text(" "))
            if n and n >= 100:
                return n
    found = [parse_number(m.group(1)) for m in PRICE_RE.finditer(card.get_text(" "))]
    found = [n for n in found if n and n >= 100]
    return min(found) if found else None


def extract_image(card):
    img = card.find("img")
    if not img:
        return ""
    src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
    return "" if not src or src.startswith("data:") else urljoin(SOURCE_BASE, src)


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
            continue  # ссылка из меню или баннера, а не карточка товара
        name = ""
        for a in card.select('a[href*="/products/"]'):
            candidate = clean(a.get("title") or a.get_text(" "))
            if candidate.lower() not in JUNK_NAMES:
                name = candidate
                break
        if not name:
            img = card.find("img")
            name = clean(img.get("alt") or img.get("title") or "") if img else ""
        if not name:
            continue
        products[href] = {
            "id": href.rsplit("/", 1)[-1],
            "name": name,
            "source_price": price,
            "image": extract_image(card),
            "stock": extract_stock(card),
        }
    return list(products.values())


def scrape_category(session, slug):
    seen, items = set(), []
    for page in range(1, MAX_PAGES + 1):
        url = CATEGORY_URL.format(slug=slug) + (f"?page={page}" if page > 1 else "")
        html = fetch(session, url)
        time.sleep(PAUSE_SECONDS)
        if not html:
            break
        new = [p for p in parse_page(html) if p["id"] not in seen]
        if not new:
            break
        for p in new:
            seen.add(p["id"])
        items.extend(new)
    return items


def apply_markup(price, pricing):
    value = price * (1 + pricing.get("markup_percent", 0) / 100) + pricing.get("markup_rub", 0)
    step = max(int(pricing.get("round_to", 1) or 1), 1)
    return int(math.ceil(value / step) * step)


# ---------- 3. Сборка ----------

def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    pricing = config.get("pricing", {})
    hide_out = pricing.get("hide_out_of_stock", True)
    exclude = set(config.get("exclude_categories", []))

    previous_total = 0
    if OUTPUT_PATH.exists():
        try:
            previous_total = len(json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("products", []))
        except Exception:
            previous_total = 0

    session = requests.Session()
    log("Читаю меню источника…")
    menu = discover_menu(session, config["groups"])

    groups_out, products = [], {}
    for g in config["groups"]:
        subs = menu.get(g["id"]) or g.get("categories") or [
            {"id": g["source"], "title": g["title"], "source": g["source"]}]
        subs = [c for c in subs if c["id"] not in exclude]
        cats_out = []
        for cat in subs:
            log(f"→ {g['title']} / {cat['title']}")
            items = scrape_category(session, cat["source"])
            kept = 0
            for p in items:
                if hide_out and p["stock"] == "out":
                    continue
                entry = products.get(p["id"])
                if entry is None:
                    entry = products[p["id"]] = {
                        "id": p["id"], "name": p["name"],
                        "price": apply_markup(p["source_price"], pricing),
                        "image": p["image"], "stock": p["stock"],
                        "cats": [], "groups": [],
                    }
                if cat["id"] not in entry["cats"]:
                    entry["cats"].append(cat["id"])
                if g["id"] not in entry["groups"]:
                    entry["groups"].append(g["id"])
                kept += 1
            log(f"   найдено {len(items)}, на сайт {kept}")
            if kept:
                cats_out.append({"id": cat["id"], "title": cat["title"]})
        if cats_out:
            groups_out.append({"id": g["id"], "title": g["title"], "categories": cats_out})

    total = len(products)
    log(f"Итого товаров: {total} (было {previous_total}), разделов: {len(groups_out)}")

    if total == 0 or (previous_total and total < previous_total * MIN_SHARE_OF_PREVIOUS):
        log("Слишком мало товаров — возможно, сайт-источник изменился или недоступен. "
            "Старые цены оставлены без изменений.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": groups_out,
        "products": list(products.values()),
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("Готово: data/products.json обновлён")


if __name__ == "__main__":
    main()
