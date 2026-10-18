"""Бот каналу «Дрогобич і дрогобичани» для GitHub Actions.

Не працює постійно: GitHub запускає цей скрипт за розкладом (кожні ~10 хв).
За один запуск скрипт:
  1. обробляє нові повідомлення боту (пости адміністратора, пропозиції читачів, кнопки);
  2. публікує запланований пост, час якого настав;
  3. публікує щоденну погоду;
  4. публікує свіжі новини з RSS.
Стан (черга, список побачених новин) лежить у state.json, який workflow
шифрує й зберігає в репозиторії. У логах НЕ друкуємо жодних особистих даних.
"""
import calendar
import html
import json
import re
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser


def env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def split(value: str) -> list:
    return [x.strip() for x in value.split(",") if x.strip()]


TOKEN = env("BOT_TOKEN")
ADMIN_ID = int(env("ADMIN_ID", "0"))
CHANNEL = env("CHANNEL_ID", "@drohobych_drohobychany")
FEEDS = split(env(
    "NEWS_FEEDS",
    "https://drohobych-rada.gov.ua/feed/,"
    "https://drogmedia.net.ua/feed/,"
    "https://droginfo.com.ua/feed/",
))
FEEDS_FILTERED = split(env(
    "NEWS_FEEDS_FILTERED",
    "https://zaxid.net/rss/,"
    "https://news.google.com/rss/search?q=Дрогобич&hl=uk&gl=UA&ceid=UA:uk",
))
FEEDS_NATIONAL = split(env("NEWS_FEEDS_NATIONAL", "https://www.pravda.com.ua/rss/view_mainnews/"))
KEYWORDS = [k.lower() for k in split(env("NEWS_KEYWORDS", "дрогобич,дрогобиц"))]
WEATHER_TIME = env("WEATHER_TIME", "09:00")
SILENCE_TIME = env("SILENCE_TIME", "09:00")
FACT_TIME = env("FACT_TIME", "12:00")
DIGEST_TIME = env("DIGEST_TIME", "19:00")  # неділя
FACTS_FILE = Path(env("FACTS_FILE", "facts.txt"))
BOT_USERNAME = ""
STATE_FILE = Path(env("STATE_FILE", "state.json"))
NEWS_MAX_PER_RUN = 5
NEWS_MAX_PER_DAY = 15
NEWS_MAX_AGE_H = 48
TZ = ZoneInfo("Europe/Kyiv")
# Дрогобицький район: (назва, широта, довгота)
PLACES = [
    ("Дрогобич", 49.3544, 23.5075),
    ("Борислав", 49.2893, 23.4276),
    ("Трускавець", 49.2780, 23.5069),
    ("Стебник", 49.2964, 23.5622),
    ("Східниця", 49.2000, 23.4500),
]

RUBRICS = {
    "news": ("📰 Новини", "#Новини"),
    "history": ("🏛 Історія", "#Історія"),
    "people": ("👥 Дрогобичани", "#Дрогобичани"),
    "afisha": ("🎭 Афіша і корисне", "#Афіша_і_корисне"),
    "week": ("🌟 Дрогобичанин тижня", "#Дрогобичани #Дрогобичанин_тижня"),
}

WEATHER_CODES = {
    0: "☀️ ясно", 1: "🌤 переважно ясно", 2: "⛅️ мінлива хмарність", 3: "☁️ похмуро",
    45: "🌫 туман", 48: "🌫 паморозь", 51: "🌦 легка мряка", 53: "🌦 мряка",
    55: "🌧 сильна мряка", 61: "🌧 невеликий дощ", 63: "🌧 дощ", 65: "🌧 сильний дощ",
    71: "🌨 невеликий сніг", 73: "🌨 сніг", 75: "❄️ сильний сніг",
    80: "🌦 короткочасні дощі", 81: "🌧 зливи", 82: "⛈ сильні зливи",
    95: "⛈ гроза", 96: "⛈ гроза з градом", 99: "⛈ гроза з сильним градом",
}


# ---------- Telegram API ----------
def api(method: str, **params) -> dict:
    data = {
        k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        for k, v in params.items()
        if v is not None
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=urllib.parse.urlencode(data).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": type(e).__name__}


def send(chat_id, text: str, **kw) -> dict:
    return api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", **kw)


def channel_kb():
    """Кнопка під кожним постом каналу: запрошення надсилати новини боту."""
    if not BOT_USERNAME:
        return None
    return {"inline_keyboard": [[{"text": "✍️ Є новина? Напишіть боту",
                                  "url": f"https://t.me/{BOT_USERNAME}"}]]}


def post(text: str, silent: bool = False) -> dict:
    return send(CHANNEL, text, reply_markup=channel_kb(),
                disable_notification="true" if silent else None)


# ---------- стан ----------
def load_state() -> dict:
    st = {"offset": 0, "seen": None, "scheduled": [], "pending": {},
          "waiting": None, "weather_date": "", "week_news": [],
          "digest_date": "", "fact_date": "", "fact_idx": 0,
          "news_day": "", "news_n": 0, "backfilled": False,
          "nat_hour": "", "nat_seen": [], "nat_init": False, "silence_date": ""}
    if STATE_FILE.exists():
        st.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return st


def save_state(st: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


# ---------- клавіатури ----------
def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def rubric_kb(chat_id, msg_id, mode: str = "now") -> dict:
    """mode=now: кнопка рубрики публікує одразу; mode=sched: веде до вибору часу."""
    prefix = "pub" if mode == "now" else "sp"
    rows = [[btn(label, f"{prefix}:{key}:{chat_id}:{msg_id}")] for key, (label, _) in RUBRICS.items()]
    rows.append([btn("📤 Без рубрики", f"{prefix}:none:{chat_id}:{msg_id}"),
                 btn("❌ Відхилити", f"rej:x:{chat_id}:{msg_id}")])
    if mode == "now":
        rows.append([btn("🕒 Запланувати на потім", f"sch:x:{chat_id}:{msg_id}")])
    return {"inline_keyboard": rows}


def when_kb(rubric: str, chat_id, msg_id) -> dict:
    def b(text, w):
        return btn(text, f"t:{w}:{rubric}:{chat_id}:{msg_id}")

    return {"inline_keyboard": [
        [b("🕕 Сьогодні 18:00", "today18")],
        [b("🌅 Завтра 09:00", "tom09"), b("🌇 Завтра 18:00", "tom18")],
        [b("✏️ Вказати свій час", "custom")],
    ]}


# ---------- пости ----------
def tag_for(rubric: str) -> str:
    return RUBRICS[rubric][1] if rubric in RUBRICS else ""


def remember(st: dict, m: dict) -> str:
    is_text = "text" in m
    key = f"{m['chat']['id']}:{m['message_id']}"
    st["pending"][key] = {
        "chat_id": m["chat"]["id"],
        "msg_id": m["message_id"],
        "is_text": is_text,
        "text": (m.get("text") if is_text else m.get("caption")) or "",
        "entities": (m.get("entities") if is_text else m.get("caption_entities")) or [],
        "ts": time.time(),
    }
    if len(st["pending"]) > 200:  # прибираємо найстаріші
        for k in sorted(st["pending"], key=lambda k: st["pending"][k]["ts"])[:50]:
            del st["pending"][k]
    return key


def make_item(p: dict, tag: str) -> dict:
    item = {k: p[k] for k in ("chat_id", "msg_id", "is_text", "text", "entities")}
    item.update(id=uuid.uuid4().hex[:6], tag=tag)
    return item


def publish_item(item: dict) -> dict:
    suffix = f"\n\n{item['tag']}" if item["tag"] else ""
    text = item["text"] + suffix if item["text"] else suffix.lstrip()
    # сутності (жирний, посилання) лишаються валідними, бо тег додаємо в кінець
    if item["is_text"]:
        return api("sendMessage", chat_id=CHANNEL, text=text,
                   entities=item["entities"] or None, reply_markup=channel_kb())
    return api("copyMessage", chat_id=CHANNEL, from_chat_id=item["chat_id"],
               message_id=item["msg_id"], caption=text,
               caption_entities=item["entities"] or None, reply_markup=channel_kb())


def preset_when(w: str) -> datetime:
    now = datetime.now(TZ)
    if w == "today18":
        return now.replace(hour=18, minute=0, second=0, microsecond=0)
    base = now + timedelta(days=1)
    return base.replace(hour=9 if w == "tom09" else 18, minute=0, second=0, microsecond=0)


def fmt_when(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m о %H:%M")


def schedule(st: dict, item: dict, when: datetime) -> None:
    item["when"] = when.isoformat()
    st["scheduled"].append(item)


# ---------- обробка повідомлень ----------
def handle_message(st: dict, m: dict) -> None:
    chat = m["chat"]
    if chat.get("type") != "private":
        return
    user = m.get("from") or {}
    uid = user.get("id")
    is_admin = bool(ADMIN_ID) and uid == ADMIN_ID
    text = m.get("text") or ""

    if text.startswith("/"):
        cmd, _, args = text.partition(" ")
        cmd, args = cmd.split("@")[0].lower(), args.strip()
        if cmd == "/id":
            send(chat["id"], f"Ваш ID: <code>{uid}</code>")
        elif cmd == "/start":
            if is_admin:
                send(chat["id"],
                     "Ви адміністратор каналу.\n\n"
                     "• Надішліть мені пост (текст, фото, відео): оберіть рубрику й час\n"
                     "• /announce <i>текст</i>: оголошення одразу\n"
                     "• /weather: погода зараз\n"
                     "• /poll <i>Питання | варіант 1 | варіант 2</i>: опитування в канал\n"
                     "• /fact: факт про Дрогобич зараз\n"
                     "• /queue: заплановане, /cancel <i>id</i>: скасувати\n\n"
                     "Бот відповідає з затримкою до ~10 хв.")
            else:
                send(chat["id"],
                     "Вітаю! Це бот каналу «Дрогобич і дрогобичани».\n"
                     "Надішліть новину чи оголошення одним повідомленням: після "
                     "перевірки модератором воно з'явиться в каналі. Відповідь може "
                     "прийти не одразу.")
        elif is_admin and cmd == "/weather":
            post(weather_text())
            send(chat["id"], "✅ Погоду опубліковано.")
        elif is_admin and cmd == "/poll":
            parts = [x.strip() for x in args.split("|") if x.strip()]
            if len(parts) < 3 or len(parts) > 11:
                send(chat["id"], "Використання: /poll Питання | варіант 1 | варіант 2 "
                                 "(від 2 до 10 варіантів)")
            else:
                res = api("sendPoll", chat_id=CHANNEL, question=parts[0][:300],
                          options=[o[:100] for o in parts[1:]], is_anonymous="true",
                          reply_markup=channel_kb())
                send(chat["id"], "✅ Опитування опубліковано." if res.get("ok")
                     else f"⚠️ Не вдалося: {html.escape(res.get('description', ''))}")
        elif is_admin and cmd == "/fact":
            send(chat["id"], "✅ Факт опубліковано." if post_fact(st)
                 else "Файл facts.txt порожній або публікація не вдалась.")
        elif is_admin and cmd == "/announce":
            if not args:
                send(chat["id"], "Використання: /announce текст")
            else:
                post(f"📢 <b>Оголошення</b>\n\n{html.escape(args)}\n\n#Афіша_і_корисне")
                send(chat["id"], "✅ Оголошення опубліковано.")
        elif is_admin and cmd == "/queue":
            items = sorted(st["scheduled"], key=lambda i: i["when"])
            if not items:
                send(chat["id"], "Черга порожня.")
            else:
                lines = [
                    f"<code>{i['id']}</code> · {fmt_when(datetime.fromisoformat(i['when']))} · "
                    f"{i['tag'] or 'без рубрики'}\n"
                    f"{html.escape((i['text'] or '[медіа]').replace(chr(10), ' ')[:50])}"
                    for i in items
                ]
                send(chat["id"], "🗓 <b>Заплановано:</b>\n\n" + "\n\n".join(lines)
                     + "\n\nСкасувати: /cancel <i>id</i>")
        elif is_admin and cmd == "/cancel":
            left = [i for i in st["scheduled"] if i["id"] != args]
            send(chat["id"], "🗑 Скасовано." if len(left) != len(st["scheduled"])
                 else "Такого id немає в черзі.")
            st["scheduled"] = left
        return

    if is_admin:
        waiting = st.get("waiting")
        if waiting and text:
            st["waiting"] = None
            try:
                when = datetime.strptime(text.strip(), "%d.%m %H:%M").replace(
                    year=datetime.now(TZ).year, tzinfo=TZ)
            except ValueError:
                when = None  # не дата: вважаємо це новим постом
            if when is not None:
                p = st["pending"].pop(waiting["key"], None)
                if p is None:
                    send(chat["id"], "⚠️ Повідомлення втрачено, надішліть його ще раз.")
                elif when <= datetime.now(TZ):
                    st["pending"][waiting["key"]] = p
                    st["waiting"] = waiting
                    send(chat["id"], "Цей час уже минув. Вкажіть майбутній: ДД.ММ ГГ:ХХ")
                else:
                    schedule(st, make_item(p, tag_for(waiting["rubric"])), when)
                    send(chat["id"], f"🕒 Заплановано на {fmt_when(when)}.")
                return
        remember(st, m)
        send(chat["id"], "Оберіть рубрику: пост опублікується одразу. Або заплануйте.",
             reply_markup=rubric_kb(chat["id"], m["message_id"]))
        return

    # пропозиція від читача
    if not ADMIN_ID:
        send(chat["id"], "Бот ще не налаштований.")
        return
    api("copyMessage", chat_id=ADMIN_ID, from_chat_id=chat["id"], message_id=m["message_id"])
    remember(st, m)
    who = html.escape(user.get("username") or "—")
    send(ADMIN_ID, f"Пропозиція від @{who} (ID {uid}). Опублікувати?",
         reply_markup=rubric_kb(chat["id"], m["message_id"]))
    send(chat["id"], "Дякуємо! Передано на модерацію.")


def handle_callback(st: dict, cq: dict) -> None:
    qid = cq["id"]
    if cq["from"]["id"] != ADMIN_ID:
        api("answerCallbackQuery", callback_query_id=qid,
            text="Лише для адміністратора", show_alert="true")
        return
    api("answerCallbackQuery", callback_query_id=qid)
    msg = cq["message"]
    parts = cq["data"].split(":")

    def edit(text, markup=None):
        api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
            text=text, parse_mode="HTML", reply_markup=markup)

    lost = "⚠️ Повідомлення втрачено. Надішліть його ще раз."
    if parts[0] == "rej":
        st["pending"].pop(f"{parts[2]}:{parts[3]}", None)
        edit("❌ Відхилено.")
    elif parts[0] == "pub":  # одразу публікуємо в обрану рубрику
        _, rubric, chat_id, msg_id = parts
        key = f"{chat_id}:{msg_id}"
        p = st["pending"].get(key)
        if p is None:
            edit(lost)
        else:
            tag = tag_for(rubric)
            res = publish_item(make_item(p, tag))
            if res.get("ok"):
                st["pending"].pop(key, None)
                edit(f"✅ Опубліковано {tag}".strip())
            else:
                edit(f"⚠️ Не вдалося опублікувати: {html.escape(res.get('description', ''))}")
    elif parts[0] == "sch":  # планування: спершу рубрика
        _, _, chat_id, msg_id = parts
        if f"{chat_id}:{msg_id}" not in st["pending"]:
            edit(lost)
        else:
            edit("Оберіть рубрику для запланованого поста:",
                 rubric_kb(chat_id, msg_id, "sched"))
    elif parts[0] == "sp":  # потім час
        _, rubric, chat_id, msg_id = parts
        if f"{chat_id}:{msg_id}" not in st["pending"]:
            edit(lost)
        else:
            edit("Коли публікувати?", when_kb(rubric, chat_id, msg_id))
    elif parts[0] == "t":
        _, w, rubric, chat_id, msg_id = parts
        key = f"{chat_id}:{msg_id}"
        p = st["pending"].get(key)
        if p is None:
            edit(lost)
            return
        tag = tag_for(rubric)
        if w == "now":
            st["pending"].pop(key)
            res = publish_item(make_item(p, tag))
            edit(f"✅ Опубліковано {tag}".strip() if res.get("ok")
                 else f"⚠️ Не вдалося опублікувати: {html.escape(res.get('description', ''))}")
        elif w == "custom":
            st["waiting"] = {"key": key, "rubric": rubric}
            edit("Надішліть дату й час у форматі <code>ДД.ММ ГГ:ХХ</code>, "
                 "наприклад <code>20.10 18:00</code>")
        else:
            when = preset_when(w)
            if when <= datetime.now(TZ):
                edit("Цей час уже минув. Оберіть інший:", when_kb(rubric, chat_id, msg_id))
                return
            st["pending"].pop(key)
            schedule(st, make_item(p, tag), when)
            edit(f"🕒 Заплановано на {fmt_when(when)} {tag}".strip())


# ---------- етапи запуску ----------
def run_updates(st: dict) -> None:
    kwargs = dict(offset=st["offset"] or None, limit=100, timeout=0,
                  allowed_updates=["message", "callback_query"])
    res = api("getUpdates", **kwargs)
    if not res.get("ok") and "webhook" in res.get("description", "").lower():
        api("deleteWebhook")
        res = api("getUpdates", **kwargs)
    if not res.get("ok"):
        print("getUpdates:", res.get("description"))
        return
    for upd in res["result"]:
        st["offset"] = upd["update_id"] + 1
        try:
            if "callback_query" in upd:
                handle_callback(st, upd["callback_query"])
            elif "message" in upd:
                handle_message(st, upd["message"])
        except Exception:
            traceback.print_exc()
    print(f"оновлень оброблено: {len(res['result'])}")


def run_scheduled(st: dict) -> None:
    now = datetime.now(TZ)
    due = [i for i in st["scheduled"] if datetime.fromisoformat(i["when"]) <= now]
    if not due:
        return
    st["scheduled"] = [i for i in st["scheduled"] if i not in due]  # спершу прибираємо
    for item in sorted(due, key=lambda i: i["when"]):
        res = publish_item(item)
        if not res.get("ok") and ADMIN_ID:
            send(ADMIN_ID, f"⚠️ Не вдалося опублікувати запланований пост "
                           f"<code>{item['id']}</code>: {html.escape(res.get('description', ''))}")
        time.sleep(2)
    print(f"заплановано опубліковано: {len(due)}")


def weather_text() -> str:
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(p[1]) for p in PLACES),
        "longitude": ",".join(str(p[2]) for p in PLACES),
        "timezone": "Europe/Kyiv", "forecast_days": 1,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"})
    with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=20) as r:
        data = json.load(r)
    if isinstance(data, dict):
        data = [data]
    lines = []
    for (name, _, _), loc in zip(PLACES, data):
        d = loc["daily"]
        desc = WEATHER_CODES.get(d["weathercode"][0], "🌡 без опису")
        lines.append(f"📍 <b>{name}</b>: {desc}\n"
                     f"    вдень {d['temperature_2m_max'][0]:+.0f}°, вночі {d['temperature_2m_min'][0]:+.0f}°, "
                     f"💧 {d['precipitation_sum'][0]:.1f} мм")
    return (f"<b>Погода на Дрогобиччині, {datetime.now(TZ):%d.%m.%Y}</b>\n\n"
            + "\n\n".join(lines) + "\n\n#Афіша_і_корисне #Погода")


def run_weather(st: dict) -> None:
    now = datetime.now(TZ)
    hour, minute = map(int, WEATHER_TIME.split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    # публікуємо в межах 3 годин після призначеного часу, щоб не слати пізню погоду
    if st["weather_date"] != today and at <= now < at + timedelta(hours=3):
        if post(weather_text()).get("ok"):
            st["weather_date"] = today
            print("погоду опубліковано")


def matches(entry) -> bool:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    return any(k in text for k in KEYWORDS)


def clean_summary(entry, limit: int = 280) -> str:
    raw = entry.get("summary", "") or ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*(Читати далі|Read more|The post).*$", "", text, flags=re.I).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def run_news(st: dict) -> None:
    first_run = st["seen"] is None
    seen_list = list(st["seen"] or [])
    seen = set(seen_list)
    pending = []
    sources = [(u, False, False) for u in FEEDS] + [(u, True, False) for u in FEEDS_FILTERED]
    for url, filtered, national in sources:
        feed = feedparser.parse(url)
        source = feed.feed.get("title", "")
        print(f"стрічка {url.split('/')[2]}: записів {len(feed.entries)}"
              + ("" if feed.entries else f" (проблема: {getattr(feed, 'bozo_exception', None) or getattr(feed, 'status', '?')})"))
        for entry in reversed(feed.entries):  # від старіших до новіших
            uid = entry.get("id") or entry.get("link")
            if not uid or uid in seen:
                continue
            if filtered and not matches(entry):
                seen.add(uid)
                seen_list.append(uid)
                continue
            pending.append((uid, source, entry))
    posted = 0
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if st["news_day"] != today:
        st["news_day"], st["news_n"] = today, 0

    def publish(entry, source, national=False) -> bool:
        title = html.escape(entry.get("title", "Без назви"))
        summary = clean_summary(entry)
        if summary.lower().startswith(entry.get("title", "").lower()[:40]):
            summary = ""  # анотація лише повторює заголовок
        body = f"\n\n{html.escape(summary)}" if summary else ""
        link = entry.get("link", "")
        src = html.escape(entry.get("source", {}).get("title") or source or "джерело")
        res = post(f"📰 <b>{title}</b>{body}\n\n"
                   f'🔗 <a href="{html.escape(link, quote=True)}">Читати: {src}</a>\n\n'
                   + ("#Новини #Україна" if national else "#Новини"))
        if not res.get("ok"):
            print("новину не опубліковано:", res.get("description"))
            return False
        st["week_news"].append({"title": entry.get("title", ""), "link": link,
                                "ts": time.time()})
        time.sleep(2)
        return True

    def too_old(entry) -> bool:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        return bool(t) and time.time() - calendar.timegm(t) > NEWS_MAX_AGE_H * 3600

    if first_run:  # перший запуск: не засмічуємо канал старими новинами
        for uid, _, _ in pending:
            seen_list.append(uid)
    else:
        if not st["backfilled"]:  # один раз: щоб канал не був порожнім
            st["backfilled"] = True
            for url in FEEDS[:1]:
                feed = feedparser.parse(url)
                for entry in list(feed.entries)[:3][::-1]:
                    if publish(entry, feed.feed.get("title", "")):
                        posted += 1
                        st["news_n"] += 1
        for uid, source, entry in pending[:NEWS_MAX_PER_RUN]:
            if too_old(entry):
                seen_list.append(uid)
                continue
            if st["news_n"] >= NEWS_MAX_PER_DAY:
                break  # ліміт на день вичерпано, решта підуть завтра
            if not publish(entry, source):
                break
            seen_list.append(uid)
            st["news_n"] += 1
            posted += 1
    st["seen"] = seen_list[-3000:]
    st["week_news"] = [n for n in st["week_news"] if time.time() - n["ts"] < 8 * 86400][-60:]
    print(f"новин опубліковано: {posted}")


def load_facts() -> list:
    if not FACTS_FILE.exists():
        return []
    return [ln.strip() for ln in FACTS_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def post_fact(st: dict) -> bool:
    facts = load_facts()
    if not facts:
        return False
    idx = st.get("fact_idx", 0) % len(facts)
    res = post(f"🏛 <b>Дрогобич: факт дня</b>\n\n{html.escape(facts[idx])}\n\n#Історія #Факт_дня")
    if res.get("ok"):
        st["fact_idx"] = idx + 1
        return True
    print("факт не опубліковано:", res.get("description"))
    return False


def in_window(now: datetime, hhmm: str, hours: int = 3) -> bool:
    h, m = map(int, hhmm.split(":"))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return at <= now < at + timedelta(hours=hours)


def run_fact(st: dict) -> None:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if st["fact_date"] != today and in_window(now, FACT_TIME):
        if post_fact(st):
            st["fact_date"] = today
            print("факт дня опубліковано")


def run_digest(st: dict) -> None:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.weekday() != 6 or st["digest_date"] == today or not in_window(now, DIGEST_TIME):
        return
    st["digest_date"] = today
    week = [n for n in st["week_news"] if time.time() - n["ts"] < 7 * 86400 and n["link"]]
    if not week:
        return
    local = [n for n in week if not n.get("nat")][-3:]  # місцевим віддаємо перевагу
    rest = [n for n in week if n.get("nat")][-(5 - len(local)):] if len(local) < 5 else []
    top = (local + rest)[-5:][::-1]
    lines = [f'{i}. <a href="{html.escape(n["link"], quote=True)}">{html.escape(n["title"])}</a>'
             for i, n in enumerate(top, 1)]
    post("🗞 <b>Головне за тиждень</b>\n\n" + "\n\n".join(lines) + "\n\n#Новини #Дайджест")
    print("дайджест опубліковано")


def silence_text() -> str:
    return ("🕯 <b>9:00. Хвилина мовчання</b>\n\n"
            "Щодня о 9:00 зупиняємося, щоб вшанувати пам'ять усіх, хто загинув, "
            "захищаючи Україну, і мирних людей, життя яких обірвала війна.\n\n"
            "Вічна пам'ять Героям. 🇺🇦\n\n#Хвилина_мовчання")


def run_silence(st: dict) -> None:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    h, m = map(int, SILENCE_TIME.split(":"))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    # нагадування має сенс лише поруч із 9:00: від -5 до +30 хвилин
    if st["silence_date"] != today and at - timedelta(minutes=5) <= now < at + timedelta(minutes=30):
        if post(silence_text()).get("ok"):
            st["silence_date"] = today
            print("хвилину мовчання опубліковано")
            time.sleep(2)


def run_national(st: dict) -> None:
    """Раз на годину: головна новина України з «Української правди»."""
    now = datetime.now(TZ)
    hour_key = now.strftime("%Y-%m-%d %H")
    if st["nat_hour"] == hour_key:
        return
    entries = []
    for url in FEEDS_NATIONAL:
        feed = feedparser.parse(url)
        print(f"стрічка {url.split('/')[2]}: записів {len(feed.entries)}")
        for e in feed.entries:
            uid = e.get("id") or e.get("link")
            if uid:
                entries.append((uid, feed.feed.get("title", ""), e))
    if not entries:
        return
    entries.sort(key=lambda x: x[2].get("published_parsed") or time.gmtime(0), reverse=True)
    if not st["nat_init"]:  # перший запуск: лише запам'ятовуємо
        st["nat_init"] = True
        st["nat_seen"] = [x[0] for x in entries][-500:]
        return
    seen = set(st["nat_seen"])
    fresh = [x for x in entries if x[0] not in seen]
    if not fresh:
        return
    uid, source, e = fresh[0]
    title = html.escape(e.get("title", "Без назви"))
    summary = clean_summary(e)
    if summary.lower().startswith(e.get("title", "").lower()[:40]):
        summary = ""
    body = f"\n\n{html.escape(summary)}" if summary else ""
    link = html.escape(e.get("link", ""), quote=True)
    silent = now.hour >= 23 or now.hour < 7  # вночі без звуку
    res = post(f"🇺🇦 <b>{title}</b>{body}\n\n"
               f'🔗 <a href="{link}">Читати: {html.escape(source or "Українська правда")}</a>'
               f"\n\n#Новини #Україна", silent=silent)
    if res.get("ok"):
        st["nat_hour"] = hour_key
        st["nat_seen"] = (st["nat_seen"] + [x[0] for x in fresh])[-500:]
        st["week_news"].append({"title": e.get("title", ""), "link": e.get("link", ""),
                                "ts": time.time(), "nat": True})
        print("новину України опубліковано")
    else:
        print("новину України не опубліковано:", res.get("description"))


def preflight() -> None:
    """Швидка перевірка налаштувань. Недійсний токен робить запуск червоним."""
    me = api("getMe")
    if not me.get("ok"):
        sys.exit(
            f"❌ BOT_TOKEN недійсний або вставлений неправильно ({me.get('description')}). "
            "Візьміть токен у @BotFather (API Token) і оновіть секрет BOT_TOKEN."
        )
    global BOT_USERNAME
    BOT_USERNAME = me["result"]["username"]
    print(f"бот: @{BOT_USERNAME}")
    print("ADMIN_ID задано:", "так" if ADMIN_ID else "НІ (секрет ADMIN_ID порожній)")
    cm = api("getChatMember", chat_id=CHANNEL, user_id=me["result"]["id"])
    if cm.get("ok"):
        r = cm["result"]
        ok = r.get("status") in ("administrator", "creator") and r.get("can_post_messages", True)
        print("бот адміністратор каналу з правом публікації:",
              "так" if ok else "НІ (додайте цього бота в адміністратори каналу)")
    else:
        print("канал недоступний боту:", cm.get("description"))


def main() -> None:
    if not TOKEN:
        sys.exit("❌ Секрет BOT_TOKEN порожній: додайте його в Settings → Secrets.")
    preflight()
    st = load_state()
    try:
        for stage in (run_updates, run_scheduled, run_silence, run_weather, run_fact,
                      run_news, run_national, run_digest):
            try:
                stage(st)
            except Exception:
                print(f"помилка на етапі {stage.__name__}")
                traceback.print_exc()
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
