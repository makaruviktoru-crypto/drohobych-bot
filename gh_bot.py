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
import html
import json
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
FEEDS = split(env("NEWS_FEEDS", "https://drohobych-rada.gov.ua/feed/"))
FEEDS_FILTERED = split(env("NEWS_FEEDS_FILTERED", "https://zaxid.net/rss/"))
KEYWORDS = [k.lower() for k in split(env("NEWS_KEYWORDS", "дрогобич,дрогобиц"))]
WEATHER_TIME = env("WEATHER_TIME", "07:30")
STATE_FILE = Path(env("STATE_FILE", "state.json"))
NEWS_MAX_PER_RUN = 5
TZ = ZoneInfo("Europe/Kyiv")
LAT, LON = 49.3544, 23.5075  # Дрогобич

RUBRICS = {
    "news": ("📰 Новини", "#Новини"),
    "history": ("🏛 Історія", "#Історія"),
    "people": ("👥 Дрогобичани", "#Дрогобичани"),
    "afisha": ("🎭 Афіша і корисне", "#Афіша_і_корисне"),
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


# ---------- стан ----------
def load_state() -> dict:
    st = {"offset": 0, "seen": None, "scheduled": [], "pending": {},
          "waiting": None, "weather_date": ""}
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


def rubric_kb(chat_id, msg_id) -> dict:
    rows = [[btn(label, f"pub:{key}:{chat_id}:{msg_id}")] for key, (label, _) in RUBRICS.items()]
    rows.append([btn("📤 Без рубрики", f"pub:none:{chat_id}:{msg_id}"),
                 btn("❌ Відхилити", f"rej:x:{chat_id}:{msg_id}")])
    return {"inline_keyboard": rows}


def when_kb(rubric: str, chat_id, msg_id) -> dict:
    def b(text, w):
        return btn(text, f"t:{w}:{rubric}:{chat_id}:{msg_id}")

    return {"inline_keyboard": [
        [b("🚀 Зараз", "now")],
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
                   entities=item["entities"] or None)
    return api("copyMessage", chat_id=CHANNEL, from_chat_id=item["chat_id"],
               message_id=item["msg_id"], caption=text,
               caption_entities=item["entities"] or None)


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
                     "• /queue: заплановане, /cancel <i>id</i>: скасувати\n\n"
                     "Бот відповідає з затримкою до ~10 хв.")
            else:
                send(chat["id"],
                     "Вітаю! Це бот каналу «Дрогобич і дрогобичани».\n"
                     "Надішліть новину чи оголошення одним повідомленням: після "
                     "перевірки модератором воно з'явиться в каналі. Відповідь може "
                     "прийти не одразу.")
        elif is_admin and cmd == "/weather":
            send(CHANNEL, weather_text())
            send(chat["id"], "✅ Погоду опубліковано.")
        elif is_admin and cmd == "/announce":
            if not args:
                send(chat["id"], "Використання: /announce текст")
            else:
                send(CHANNEL, f"📢 <b>Оголошення</b>\n\n{html.escape(args)}\n\n#Афіша_і_корисне")
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
        send(chat["id"], "Оберіть рубрику для публікації:",
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
    elif parts[0] == "pub":
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
        "latitude": LAT, "longitude": LON, "timezone": "Europe/Kyiv", "forecast_days": 1,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"})
    with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=20) as r:
        d = json.load(r)["daily"]
    desc = WEATHER_CODES.get(d["weathercode"][0], "🌡 без опису")
    return (f"<b>Погода в Дрогобичі, {datetime.now(TZ):%d.%m.%Y}</b>\n\n{desc}\n"
            f"🌡 вдень {d['temperature_2m_max'][0]:+.0f}°, вночі {d['temperature_2m_min'][0]:+.0f}°\n"
            f"💧 опади: {d['precipitation_sum'][0]:.1f} мм\n\n#Афіша_і_корисне")


def run_weather(st: dict) -> None:
    now = datetime.now(TZ)
    hour, minute = map(int, WEATHER_TIME.split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    # публікуємо в межах 3 годин після призначеного часу, щоб не слати пізню погоду
    if st["weather_date"] != today and at <= now < at + timedelta(hours=3):
        if send(CHANNEL, weather_text()).get("ok"):
            st["weather_date"] = today
            print("погоду опубліковано")


def matches(entry) -> bool:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    return any(k in text for k in KEYWORDS)


def run_news(st: dict) -> None:
    first_run = st["seen"] is None
    seen_list = list(st["seen"] or [])
    seen = set(seen_list)
    pending = []
    for url, filtered in [(u, False) for u in FEEDS] + [(u, True) for u in FEEDS_FILTERED]:
        feed = feedparser.parse(url)
        source = feed.feed.get("title", "")
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
    if first_run:  # перший запуск: не засмічуємо канал старими новинами
        for uid, _, _ in pending:
            seen_list.append(uid)
    else:
        for uid, source, entry in pending[:NEWS_MAX_PER_RUN]:
            title = html.escape(entry.get("title", "Без назви"))
            res = send(CHANNEL, f"📰 <b>{title}</b>\n\n{html.escape(source)}\n"
                                f"{entry.get('link', '')}\n\n#Новини")
            if not res.get("ok"):
                print("новину не опубліковано:", res.get("description"))
                break
            seen_list.append(uid)
            posted += 1
            time.sleep(2)
    st["seen"] = seen_list[-3000:]
    print(f"новин опубліковано: {posted}")


def main() -> None:
    if not TOKEN:
        sys.exit("BOT_TOKEN не задано")
    st = load_state()
    try:
        for stage in (run_updates, run_scheduled, run_weather, run_news):
            try:
                stage(st)
            except Exception:
                print(f"помилка на етапі {stage.__name__}")
                traceback.print_exc()
    finally:
        save_state(st)


if __name__ == "__main__":
    main()

