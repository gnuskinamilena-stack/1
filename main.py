# main.py
# Требуется: pip install aiogram aiohttp python-dotenv
import asyncio, os, re, json, time
from pathlib import Path
from statistics import mean
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

# ---------------- ENV ----------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

CHECK_INTERVAL = 5          # каждые 5 секунд
ANOMALY_DROP_PERCENT = 70   # падение от средней, % (>= 70%)
NEED_HISTORY_MIN = 3        # минимум накопленных цен для расчета средней
REPOST_AFTER_SEC = 24 * 3600
PRICE_MEMORY = 50

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# ---------------- TELEGRAM ----------------
bot = Bot(TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer("✅ Бот работает: мониторю Wildberries + Яндекс.Маркет. Публикую аномально дешёвые товары.")

# ---------------- STORAGE ----------------
DATA = Path("data"); DATA.mkdir(exist_ok=True)
WB_PRICES = DATA / "prices_wb.json"     # id -> [prices]
YM_PRICES = DATA / "prices_ym.json"     # id -> [prices]
HISTORY   = DATA / "history.json"       # key -> {"last_price": float, "last_post_ts": int}

def _load(p: Path):
    if not p.exists(): return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except:
        return {}

def _save(p: Path, obj):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)

wb_prices = _load(WB_PRICES)
ym_prices = _load(YM_PRICES)
history   = _load(HISTORY)

def _upd_price(store: dict, pid: str, price: float):
    arr = store.get(pid, [])
    arr.append(float(price))
    if len(arr) > PRICE_MEMORY:
        arr = arr[-PRICE_MEMORY:]
    store[pid] = arr

def _avg(store: dict, pid: str):
    arr = store.get(pid, [])
    return mean(arr) if arr else None

def _drop(old: float, new: float) -> float:
    if not old or old <= 0: return 0.0
    return max(0.0, (old - new) / old * 100.0)

def _may_post(key: str, price: float) -> bool:
    """Не слать дубли; если прошло 24ч — разрешить повтор."""
    now = int(time.time())
    rec = history.get(key)
    if rec is None:
        history[key] = {"last_price": price, "last_post_ts": 0}
        return True
    # если цена не менялась и пост был < 24ч назад — не дублируем
    if abs(rec["last_price"] - price) < 1e-6 and (now - rec["last_post_ts"]) < REPOST_AFTER_SEC:
        return False
    # иначе можно постить (или прошло 24ч, или цена новая)
    return True

def _mark_posted(key: str, price: float):
    history[key] = {"last_price": price, "last_post_ts": int(time.time())}
    _save(HISTORY, history)

# ---------------- FETCHERS ----------------
async def fetch_wb(session: aiohttp.ClientSession, query: str, page: int = 1):
    """Поиск WB: берём выдачу и тянем цену/ссылку. Изображение отдадим через превью ссылки."""
    url = ("https://search.wb.ru/exactmatch/ru/common/v4/search"
           f"?appType=1&curr=rub&dest=-1257786&query={query}&page={page}")
    async with session.get(url, headers=HEADERS, timeout=20) as r:
        data = await r.json(content_type=None)
    prods = data.get("data", {}).get("products", []) or []
    out = []
    for p in prods:
        pid = str(p.get("id"))
        name = p.get("name") or "Товар"
        price = (p.get("salePriceU") or 0) / 100
        if price <= 0:
            continue
        link = f"https://www.wildberries.ru/catalog/{pid}/detail.aspx"
        out.append({
            "key": f"wb:{pid}",
            "id": pid,
            "name": name,
            "price": float(price),
            "source": "Wildberries",
            "link": link
        })
    return out

async def fetch_ym(session: aiohttp.ClientSession, query: str, page: int = 1):
    """Лёгкий парсер Я.Маркета — тянем цены из поисковой страницы, без тяжёлых браузеров."""
    url = f"https://market.yandex.ru/search?text={query}&page={page}"
    async with session.get(url, headers=HEADERS, timeout=25) as r:
        html = await r.text()
    out = []
    blocks = html.split('data-auto="serp-item"')
    # цена: aria-label="... 12 345 ₽"; id: data-sku= или data-product-id=
    price_re = re.compile(r'aria-label="[^"]*?(\d[\d\s]+)\s*₽"', re.U)
    id_re    = re.compile(r'data-(?:sku|product-id)="([^"]+)"')
    name_re  = re.compile(r'(?:title|alt)="([^"]{10,160})"')
    for b in blocks[:40]:
        mprice = price_re.search(b)
        mid    = id_re.search(b)
        if not (mprice and mid):
            continue
        price = float(re.sub(r"[^\d]", "", mprice.group(1)))
        pid = mid.group(1)
        mname = name_re.search(b)
        name = mname.group(1).strip() if mname else "Товар"
        link = f"https://market.yandex.ru/product--{pid}"
        out.append({
            "key": f"ym:{pid}",
            "id": pid,
            "name": name,
            "price": price,
            "source": "Яндекс.Маркет",
            "link": link
        })
    return out

# ---------------- CORE ----------------
POPULAR_QUERIES = [
    # широкий охват категорий
    "смартфон","ноутбук","планшет","наушники","телевизор","колонка","пылесос","кресло","стул",
    "кроссовки","ботинки","куртка","платье","джинсы","игрушка","конструктор","LEGO",
    "духи","шампунь","крем","чайник","кофеварка","микроволновка","блендер","SSD","видеокарта",
    "apple","samsung","xiaomi","dyson","philips","sony"
]

async def process_items(items, prices_store: dict, store_name: str):
    """Обновляем историю цен, проверяем аномалию и публикуем посты."""
    posted = 0
    for it in items:
        key, name, price, source, link = it["key"], it["name"], it["price"], it["source"], it["link"]

        # обновить историю
        _upd_price(prices_store, it["id"], price)
        avg_prev = _avg(prices_store, it["id"])
        # если истории мало, не публикуем (сначала набираем базу 2-3 итерации)
        if len(prices_store.get(it["id"], [])) < NEED_HISTORY_MIN:
            continue

        drop_pct = _drop(avg_prev, price)
        if drop_pct < ANOMALY_DROP_PERCENT:
            continue

        # антидубль / 24ч
        if not _may_post(key, price):
            continue

        text = (
            "🔥 <b>Аномально низкая цена</b>\n"
            f"📦 <b>{name}</b>\n"
            f"💸 Цена: <b>{int(price):,} ₽</b>\n"
            f"📉 Падение от средней: <b>−{int(drop_pct)}%</b>\n"
            f"🛒 Магазин: {source}\n"
            f"🔗 <a href='{link}'>Открыть товар</a>"
        ).replace(",", " ")

        try:
            # ВАЖНО: не отключаем превью — Telegram подгрузит фото карточки по ссылке
            await bot.send_message(CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=False)
            _mark_posted(key, price)
            posted += 1
            print(f"[POST] {source}: {name[:50]}... {price} ₽ (−{int(drop_pct)}%)")
        except Exception as e:
            print("TG send error:", e)

    # сохранить историю цен
    if store_name == "wb":
        _save(WB_PRICES, prices_store)
    else:
        _save(YM_PRICES, prices_store)
    if posted:
        _save(HISTORY, history)

async def monitor_loop():
    async with aiohttp.ClientSession() as s:
        qi = 0
        while True:
            query = POPULAR_QUERIES[qi % len(POPULAR_QUERIES)]
            qi += 1
            try:
                print(f"[WB] Проверяю: {query}")
                wb_items = await fetch_wb(s, query, page=1)
                await process_items(wb_items, wb_prices, "wb")
            except Exception as e:
                print("WB fetch error:", e)

            try:
                print(f"[YM] Проверяю: {query}")
                ym_items = await fetch_ym(s, query, page=1)
                await process_items(ym_items, ym_prices, "ym")
            except Exception as e:
                print("YM fetch error:", e)

            await asyncio.sleep(CHECK_INTERVAL)

async def main():
    print("🤖 Бот запущен. Начинаю мониторинг WB + Яндекс.Маркет...")
    await asyncio.gather(dp.start_polling(bot), monitor_loop())

if __name__ == "__main__":
    asyncio.run(main())
