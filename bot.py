import subprocess
import sys

try:  # если библиотек нет — бот сам их установит при первом запуске
    from aiogram.client.default import DefaultBotProperties  # noqa: F401  (нужен aiogram >= 3.7)
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "aiogram>=3.7"])

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import BytesIO

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message,
)

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

logging.basicConfig(level=logging.INFO)

# ═══════════ НАСТРОЙКИ ═══════════
# Вставь сюда токен бота от @BotFather и свой Telegram ID (узнать: команда /id в боте
# или @userinfobot). Несколько админов — через запятую: "111,222".
# Можно вместо этого задать переменные окружения BOT_TOKEN и ADMIN_IDS.
BOT_TOKEN = ""
ADMIN_IDS_TEXT = ""
# ═════════════════════════════════

load_dotenv()
TOKEN = (os.getenv("BOT_TOKEN") or BOT_TOKEN).strip()
ADMIN_IDS = {int(x.strip()) for x in (os.getenv("ADMIN_IDS") or ADMIN_IDS_TEXT).split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ideas.sqlite3")

if not TOKEN:
    raise RuntimeError("Впиши BOT_TOKEN в начале файла bot.py (раздел НАСТРОЙКИ)")
if not ADMIN_IDS:
    raise RuntimeError("Впиши свой Telegram ID в ADMIN_IDS_TEXT в начале файла bot.py")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()
dp.message.filter(F.chat.type == "private")

# ───────────────────────── константы ─────────────────────────

KINDS = {
    "mod": ("📦", "Мод", "мода"),
    "launcher": ("🚀", "Лаунчер", "лаунчера"),
}

FILTERS = {"all": None, "free": ("new",), "taken": ("pending", "chosen")}

SUG_STATUS = {
    "open": ("🕓", "На рассмотрении"),
    "accepted": ("✅", "Принята в список"),
    "rejected": ("❌", "Отклонена"),
}

MENU_BTN = "🏠 Главное меню"  # старая кнопка: у кого она ещё висит, продолжит работать


# ───────────────────────── база данных ─────────────────────────

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def add_columns(con, table, cols):
    have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, ddl in cols.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL
        )""")
        add_columns(con, "ideas", {
            "status_by": "INTEGER", "status_by_name": "TEXT",
            "status_at": "TEXT", "source_suggestion": "INTEGER",
        })
        con.execute("""CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS requests (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            added_by INTEGER,
            added_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL,
            ref INTEGER,
            data TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS files (
            kind TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            file_unique_id TEXT,
            file_name TEXT,
            file_size INTEGER,
            caption TEXT,
            uploaded_at TEXT NOT NULL,
            uploaded_by INTEGER
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            idea_id INTEGER,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sug_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")


# Описание, которое видно в пустом чате ещё до нажатия «Запустить» (до 512 символов)
BOT_DESCRIPTION = (
    "🎨 Decide Visuals\n\n"
    "Бот клиента Decide Visuals.\n\n"
    "⬇️ Скачай мод и лаунчер\n"
    "💡 Предложи свою идею — она придёт напрямую создателю, а детали можно обсудить в чате\n"
    "📚 Подай заявку на просмотр общего списка идей\n\n"
    "Нажми «Запустить» 👇"
)
# Короткое описание: видно в профиле бота и при пересылке (до 120 символов)
BOT_SHORT_DESCRIPTION = "Клиент Decide Visuals: скачать мод и лаунчер, предложить идею 🎨"

# ───────────────────────── утилиты ─────────────────────────

def esc(s):
    return html.escape(str(s or ""), quote=False)


def esc_cut(raw, limit):
    """Экранирует текст и обрезает так, чтобы результат не превышал limit. -> (текст, обрезан?)"""
    e = esc(raw)
    if len(e) <= limit:
        return e, False
    cut = min(len(raw), limit)
    while cut > 0 and len(esc(raw[:cut])) > limit - 1:
        cut = int(cut * 0.85)
    return esc(raw[:cut]).rstrip() + "…", True


def esc_pieces(raw, limit=3500):
    """Разбивает сырой текст на экранированные строки, каждая не длиннее limit."""
    pieces = []
    for line in raw.split("\n"):
        e = esc(line)
        if len(e) <= limit:
            pieces.append(e)
        else:
            step = max(100, limit // 6)
            for i in range(0, len(line), step):
                pieces.append(esc(line[i:i + step]))
    return pieces or [""]


def pack(lines, limit=3900):
    """Склеивает строки в сообщения, не превышая лимит Telegram."""
    out, cur = [], ""
    for line in lines:
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > limit and cur:
            out.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def short(text, n=60):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def fmt_date(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y")
    except Exception:
        return "—"


def fmt_dt(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m %H:%M")
    except Exception:
        return ""


def fmt_size(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def user_link(uid, name, username=None):
    s = f'<a href="tg://user?id={uid}">{esc(name or uid)}</a>'
    if username:
        s += f" (@{esc(username)})"
    return s


def B(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def KB(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def ack(call, text=None, alert=False):
    try:
        await call.answer(text, show_alert=alert)
    except TelegramAPIError:
        pass


async def edit(call, text, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await call.message.answer(text, reply_markup=markup)
        except TelegramAPIError:
            logging.exception("Не удалось отправить сообщение")


async def present(target, text, markup=None):
    if isinstance(target, CallbackQuery):
        await edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


# ───────────────────────── сессии (режимы ввода) ─────────────────────────

def set_session(uid, mode, ref=None, data=None):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO sessions(user_id,mode,ref,data) VALUES(?,?,?,?)",
            (uid, mode, ref, json.dumps(data, ensure_ascii=False) if data is not None else None),
        )


def get_session(uid):
    with db() as con:
        row = con.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return None
    return {"mode": row["mode"], "ref": row["ref"], "data": json.loads(row["data"]) if row["data"] else None}


def clear_session(uid):
    with db() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))


# ───────────────────────── доступ ─────────────────────────

def is_admin(uid):
    if uid in ADMIN_IDS:
        return True
    with db() as con:
        return con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None


def all_admin_ids():
    with db() as con:
        extra = {r["user_id"] for r in con.execute("SELECT user_id FROM admins")}
    return ADMIN_IDS | extra


def access(uid):
    if is_admin(uid):
        return "approved"
    with db() as con:
        row = con.execute("SELECT status FROM requests WHERE user_id=?", (uid,)).fetchone()
    return row["status"] if row else None


async def need_access(call):
    if access(call.from_user.id) != "approved":
        await ack(call, "Нет доступа. Подай заявку в разделе «Идеи».", alert=True)
        return False
    return True


async def need_admin(call):
    if not is_admin(call.from_user.id):
        await ack(call, "Только для администратора", alert=True)
        return False
    return True


# ───────────────────────── файлы клиента ─────────────────────────

def get_file(kind):
    with db() as con:
        return con.execute("SELECT * FROM files WHERE kind=?", (kind,)).fetchone()


def save_file(kind, info, uid):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO files(kind,file_id,file_unique_id,file_name,file_size,caption,uploaded_at,uploaded_by)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (kind, info["file_id"], info["unique"], info["name"], info["size"], info["caption"], now(), uid),
        )


# ───────────────────────── идеи ─────────────────────────

def status_info(status):
    return {
        "new": ("🟢", "Никто не трогал"),
        "pending": ("🟢", "Выбрана, ожидает получения списка"),
        "chosen": ("🟡", "Выбрана и зафиксирована через «Получить список»"),
        "declined": ("🔴", "От идеи отказались"),
    }[status]


def idea_ids(f):
    st = FILTERS.get(f)
    q, params = "SELECT id FROM ideas", []
    if st:
        q += " WHERE status IN (%s)" % ",".join("?" * len(st))
        params = list(st)
    with db() as con:
        return [r["id"] for r in con.execute(q + " ORDER BY id", params)]


def get_idea(idea_id):
    with db() as con:
        return con.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()


def resolve_idea(f, idea_id, exact):
    if exact and idea_id:
        idea = get_idea(idea_id)
        if idea:
            return idea
    ids = idea_ids(f)
    if not ids:
        return None
    if idea_id in ids:
        target = idea_id
    else:
        bigger = [i for i in ids if i > idea_id]
        target = bigger[0] if bigger else ids[-1]
        if not idea_id:
            target = ids[0]
    return get_idea(target)


def counts_ideas():
    with db() as con:
        return {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM ideas GROUP BY status")}


def set_status(idea_id, status, user):
    by = user.id if status != "new" else None
    name = user.full_name if status != "new" else None
    with db() as con:
        con.execute(
            "UPDATE ideas SET status=?, status_by=?, status_by_name=?, status_at=? WHERE id=?",
            (status, by, name, now(), idea_id),
        )
        action = {"pending": "selected", "declined": "declined", "new": "returned"}.get(status, status)
        con.execute("INSERT INTO actions(idea_id,user_id,action,created_at) VALUES(?,?,?,?)",
                    (idea_id, user.id, action, now()))


def filter_row(f, idea_id):
    labels = {"all": "Все", "free": "🟢 Свободные", "taken": "🟡 Взятые"}
    return [B(("✔ " if k == f else "") + v, f"vf:{k}:{idea_id}") for k, v in labels.items()]


def build_card(idea, f, admin):
    iid = idea["id"]
    ids = idea_ids(f)
    if iid in ids:
        where = f"{ids.index(iid) + 1} из {len(ids)}"
    else:
        where = "вне фильтра"
    smaller = [i for i in ids if i < iid]
    larger = [i for i in ids if i > iid]
    prev_id = smaller[-1] if smaller else (ids[-1] if ids and ids[-1] != iid else None)
    next_id = larger[0] if larger else (ids[0] if ids and ids[0] != iid else None)

    emoji, label = status_info(idea["status"])
    who = ""
    if idea["status_by_name"] and idea["status"] != "new":
        verb = "Отказ" if idea["status"] == "declined" else "Взял(а)"
        who = f"\n👤 {verb}: <b>{esc(idea['status_by_name'])}</b>"
    body, cut = esc_cut(idea["text"], 2800)
    c = counts_ideas()
    taken = c.get("pending", 0) + c.get("chosen", 0)
    total = sum(c.values())
    text = (
        f"💡 <b>Идея #{iid}</b> · {where}\n\n{body}\n\n"
        f"{emoji} <b>{esc(label)}</b>{who}\n\n"
        f"<i>📊 Всего {total} · свободно {c.get('new', 0)} · взято {taken} · отказ {c.get('declined', 0)}</i>"
    )

    st = idea["status"]
    if st == "new":
        actions = [B("🟢 Добавить в список", f"pick:{f}:{iid}"), B("🔴 Отказаться", f"decl:{f}:{iid}")]
    elif st == "pending":
        actions = [B("↩️ Вернуть в список", f"ret:{f}:{iid}"), B("🔴 Отказаться", f"decl:{f}:{iid}")]
    else:
        actions = [B("↩️ Вернуть в список", f"ret:{f}:{iid}")]

    rows = [
        [B("⬅️", f"i:{f}:{prev_id}" if prev_id else "noop"),
         B(where if iid in ids else "·", "noop"),
         B("➡️", f"i:{f}:{next_id}" if next_id else "noop")],
        actions,
    ]
    if cut:
        rows.append([B("📄 Показать полностью", f"full:{iid}")])
    rows.append(filter_row(f, iid))
    # Кнопка доступна только если есть хотя бы одна выбранная идея.
    with db() as con:
        selected_count = con.execute(
            "SELECT COUNT(*) FROM ideas WHERE status IN ('pending', 'chosen')"
        ).fetchone()[0]
    if selected_count:
        rows.append([B(f"📋 Получить список ({selected_count})", f"rpt:{f}:{iid}")])
    else:
        rows.append([B("📋 Сначала выбери хотя бы одну идею", "noop")])
    if admin:
        rows.append([B("✏️ Изменить", f"edit:{iid}"), B("🗑 Удалить", f"del:{f}:{iid}")])
    rows.append([B("⬅️ В меню идей", "m:ideas")])
    return text, KB(*rows)


async def show_idea(target, f="all", idea_id=0, exact=True):
    idea = resolve_idea(f, idea_id, exact)
    if not idea:
        await present(
            target,
            "📭 <b>Здесь пока пусто</b>\n\nИдей с таким фильтром нет.",
            KB(filter_row(f, 0), [B("⬅️ В меню идей", "m:ideas")]),
        )
        return
    text, kb = build_card(idea, f, is_admin(target.from_user.id))
    await present(target, text, kb)


def parse_fi(data):
    _, f, i = data.split(":")
    return (f if f in FILTERS else "all"), int(i)


def parse_ideas(raw):
    """Нумерованные строки начинают новые идеи; можно также разделять строкой ---."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    lines = raw.split("\n")
    num = re.compile(r"^\s*\d+[.)]\s+")
    has_numbers = any(num.match(line) for line in lines)
    has_sep = any(line.strip() == "---" for line in lines)
    chunks, current, started = [], [], False
    for line in lines:
        if line.strip() == "---":
            if "\n".join(current).strip():
                chunks.append("\n".join(current).strip())
            current, started = [], True
        elif has_numbers and num.match(line):
            if "\n".join(current).strip():
                chunks.append("\n".join(current).strip())
            current, started = [num.sub("", line).strip()], True
        elif has_numbers and not has_sep and not started:
            continue  # вступление перед первым пунктом
        else:
            current.append(line)
    if "\n".join(current).strip():
        chunks.append("\n".join(current).strip())

    result, seen = [], set()
    for item in chunks:
        item = re.sub(r"\n{3,}", "\n\n", item).strip()
        key = norm_key(item)
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def norm_key(text):
    return re.sub(r"\s+", " ", text).casefold()


# ───────────────────────── поиск похожих идей (большой словарь + морфология) ─────────────────────────

STOP = set("""а без более будет будто бы в вам вас ведь во вот все всего всех вы где даже два для до его ей
ее если есть еще ещё же за зачем здесь и из или им их к как какая какой когда кто ли мне меня
может можно мой мы на надо над нами нас не него нее ней нет ни них но ну о об один он она они оно
от очень по под при про раз разве с себе себя снова со так также там тебя тем то тогда того тоже
только том тот ты у уж уже чем что чтобы эта эти это этом этот я
добавить сделай сделать хочу нужно надо можно возможность функцию feature
майнкрафт майн minecraft мод просто типа короче например пожалуйста вообще
которые который которая которое через между после перед около рядом было бы хотелось бы""".split())

try:
    from _sim_data import ROOTS as _ROOTS, SUFFIXES as _SUFFIXES, GROUPS as _CONCEPT_LIST
except ImportError:
    import importlib.util, os as _os
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_sim_data.py")
    _spec = importlib.util.spec_from_file_location("_sim_data", _p)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _ROOTS, _SUFFIXES, _CONCEPT_LIST = _mod.ROOTS, _mod.SUFFIXES, _mod.GROUPS

_ROOT_SET = set(_ROOTS)
# префиксный индекс корней для быстрого поиска без миллионов ключей
_PREFIX_INDEX = {}
for _r in _ROOTS:
    for _n in (4, 5, 6):
        if len(_r) >= _n:
            _PREFIX_INDEX.setdefault(_r[:_n], _r)

def _build_morph_index():
    """Совместимость: тяжёлый индекс больше не строится."""
    return

def _strip_suffix(token):
    t = token.lower()
    for suf in _SUFFIXES:
        if suf and len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)]
    return t

_CONCEPT_INDEX = {}
for _gi, _group in enumerate(_CONCEPT_LIST):
    for _w in _group:
        _CONCEPT_INDEX[_w] = _gi

_MICRO_BASE = len(_CONCEPT_LIST)
_ROOT_CONCEPT = {}
for _i, _r in enumerate(_ROOTS):
    if len(_r) >= 4:
        _ROOT_CONCEPT[_r] = _MICRO_BASE + _i

SYNONYMS = {
    "фото": {"изображ", "картинк", "image", "photo", "picture", "pic", "img"},
    "фон": {"background", "wallpaper", "обои", "backdrop", "bg"},
    "кастом": {"custom", "свой", "свои", "пользовательск", "personal"},
    "экран": {"screen", "gui", "меню", "интерфейс", "hud", "главн"},
    "загруж": {"upload", "load", "став", "поставить", "установ", "загрузк"},
    "прыж": {"jump", "подскок", "скач", "прыжок"},
    "авто": {"auto", "автомат", "automatic", "автоматич"},
    "карта": {"map", "minimap", "миникарт", "radar"},
    "лаг": {"lag", "тормоз", "фриз", "stutter"},
    "fps": {"фпс", "производител", "performance", "оптимиз"},
    "летать": {"fly", "полёт", "полет", "flight", "лета"},
    "дракон": {"dragon", "enderdragon"},
    "босс": {"boss"},
    "маги": {"magic", "spell", "заклин", "чар"},
    "крафт": {"craft", "recipe", "рецепт"},
    "скин": {"skin", "skinpack"},
    "руда": {"ore", "алмаз", "diamond"},
    "подсветк": {"highlight", "xray", "иксрей", "glow", "outline"},
    "моб": {"mob", "монстр", "существ", "creature"},
    "сервер": {"server", "online", "мультиплеер"},
    "звук": {"sound", "music", "аудио", "sfx"},
    "кнопк": {"button", "btn", "клавиш"},
    "меню": {"menu", "gui", "ui", "интерфейс"},
}

def words(text):
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))

def stem(w):
    w = w.lower().replace("ё", "е")
    if len(w) <= 3:
        return w
    for suf in (
        "иями","ями","ами","иях","ях","ием","ом","ем","ой","ей","ию","ью",
        "ую","юю","ая","яя","ое","ее","ые","ие","ых","их","ым","им",
        "ить","ать","ять","еть","уть","ешь","ишь","ете","ите","ают","яют",
        "ает","яет","ует","ит","ыл","ила","ило","или","ала","ало","али",
        "ние","нья","ция","ность","ение","тель","ость","ическ","еск",
        "ing","tion","ness","ment","able","ible","ers","ies","ied","ed","ly",
    ):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:6] if len(w) > 6 else w

def resolve_root(token):
    t = token.lower()
    if t in _ROOT_SET:
        return t
    st = stem(t)
    if st in _ROOT_SET:
        return st
    stripped = _strip_suffix(t)
    if stripped in _ROOT_SET:
        return stripped
    if stem(stripped) in _ROOT_SET:
        return stem(stripped)
    for n in (6, 5, 4):
        if len(st) >= n and st[:n] in _PREFIX_INDEX:
            return _PREFIX_INDEX[st[:n]]
        if len(stripped) >= n and stripped[:n] in _PREFIX_INDEX:
            return _PREFIX_INDEX[stripped[:n]]
    return st

def expand_token(t):
    out = {t, resolve_root(t)}
    root = resolve_root(t)
    for key, syns in SYNONYMS.items():
        if t.startswith(key[:4]) or key.startswith(t[:4]) or root.startswith(key[:4]) or t in syns:
            out.add(key)
            out |= set(syns)
            out.add(resolve_root(key))
    return {x for x in out if x}

def tokens(text):
    raw = [stem(w) for w in words(text) if len(w) >= 3 and w not in STOP]
    expanded = set()
    for t in raw:
        expanded |= expand_token(t)
    return expanded, set(raw)

def char_ngrams(text, n=3):
    s = re.sub(r"\s+", " ", text.lower().replace("ё", "е"))
    s = re.sub(r"[^a-zа-я0-9 ]", "", s)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}

def extract_concepts(text):
    _build_morph_index()
    found = set()
    ws = words(text)
    joined = " ".join(ws)
    compact = joined.replace(" ", "")
    for w in ws:
        st = stem(w)
        root = resolve_root(w)
        for key, gid in _CONCEPT_INDEX.items():
            if len(key) < 3:
                continue
            if st == key or w == key or root == key:
                found.add(gid)
            elif len(key) >= 4 and (st.startswith(key) or (key.startswith(st) and len(st) >= 4)):
                found.add(gid)
            elif len(key) >= 4 and key in w:
                found.add(gid)
        if root in _ROOT_CONCEPT:
            found.add(_ROOT_CONCEPT[root])
        elif st in _ROOT_CONCEPT:
            found.add(_ROOT_CONCEPT[st])
    if any(x in compact for x in ("авто", "auto")) and any(x in compact for x in ("прыж", "jump")):
        for key, gid in _CONCEPT_INDEX.items():
            if key in ("прыж", "jump", "автопрыж"):
                found.add(gid)
    has_bg = any(x in compact for x in ("фон", "wallpaper", "обои", "фото", "картин", "image", "background", "кастом", "photo"))
    has_screen = any(x in compact for x in ("экран", "screen", "меню", "gui", "главн", "интерф", "home"))
    if has_bg and has_screen:
        for key, gid in _CONCEPT_INDEX.items():
            if key in ("фон", "background", "фото", "экран", "gui", "меню", "главн"):
                found.add(gid)
    return found

def concept_similarity(ca, cb):
    if not ca or not cb:
        return 0.0
    inter = ca & cb
    if not inter:
        return 0.0
    def w(cid):
        return 1.25 if cid < _MICRO_BASE else 0.3
    w_inter = sum(w(i) for i in inter)
    w_a = sum(w(i) for i in ca) or 1
    w_b = sum(w(i) for i in cb) or 1
    w_union = sum(w(i) for i in (ca | cb)) or 1
    return 0.42 * (w_inter / w_union) + 0.58 * (w_inter / min(w_a, w_b))

def key_phrases(text):
    ws = [w for w in words(text) if len(w) >= 4 and w not in STOP]
    bigrams = [f"{ws[i]}_{ws[i+1]}" for i in range(len(ws) - 1)]
    trigrams = [f"{ws[i]}_{ws[i+1]}_{ws[i+2]}" for i in range(len(ws) - 2)]
    return set(ws[:50]) | set(bigrams[:40]) | set(trigrams[:20])

def action_object_pairs(text):
    acts = {"добав","сдела","загруз","постав","установ","включ","убери","удал","измен","настро","созда","покаж","отобра","смен","выбер","upload","add","make","set","change","custom","встав","прикреп"}
    objs = {"фон","фото","экран","прыж","карт","скин","звук","моб","босс","инвентар","меню","кнопк","лаг","fps","блок","руда","крафт","background","wallpaper","screen","jump","map","skin","обои","картин","image","photo","gui","hud"}
    ws = words(text)
    pairs = set()
    for i, w in enumerate(ws):
        st = stem(w)
        if not any(st.startswith(a) or a in st for a in acts):
            continue
        for j in range(max(0, i - 2), min(len(ws), i + 5)):
            if j == i:
                continue
            ot = stem(ws[j])
            if any(ot.startswith(o) or o in ot for o in objs):
                pairs.add((st[:4], ot[:4]))
    return pairs

def _sorted_bag(text):
    """Слова без стоп-слов, отсортированные — порядок в предложении не важен."""
    return sorted(w for w in words(text) if len(w) >= 3 and w not in STOP)


def _root_bag(text):
    """Мешок корней (порядок не важен)."""
    return sorted(resolve_root(stem(w)) for w in words(text) if len(w) >= 3 and w not in STOP)


def similarity(a, b):
    """Похожесть по смыслу; устойчива к перестановке слов."""
    if not a or not b:
        return 0.0

    ta_exp, ta_raw = tokens(a)
    tb_exp, tb_raw = tokens(b)
    ca, cb = extract_concepts(a), extract_concepts(b)
    pa, pb = key_phrases(a), key_phrases(b)
    nga, ngb = char_ngrams(a, 3), char_ngrams(b, 3)
    aa, ab = action_object_pairs(a), action_object_pairs(b)
    concept_score = concept_similarity(ca, cb)

    # --- мешок слов / корней (полностью игнорирует порядок) ---
    bag_a, bag_b = _sorted_bag(a), _sorted_bag(b)
    root_a, root_b = _root_bag(a), _root_bag(b)
    set_a, set_b = set(bag_a), set(bag_b)
    rset_a, rset_b = set(root_a), set(root_b)

    if set_a and set_b:
        bag_jac = len(set_a & set_b) / len(set_a | set_b)
        bag_cont = len(set_a & set_b) / min(len(set_a), len(set_b))
    else:
        bag_jac = bag_cont = 0.0

    if rset_a and rset_b:
        root_jac = len(rset_a & rset_b) / len(rset_a | rset_b)
        root_cont = len(rset_a & rset_b) / min(len(rset_a), len(rset_b))
    else:
        root_jac = root_cont = 0.0

    # сравнение отсортированных «канонических» фраз (одна и та же идея при перестановке)
    canon_a = " ".join(bag_a)
    canon_b = " ".join(bag_b)
    canon_seq = SequenceMatcher(None, canon_a[:500], canon_b[:500]).ratio() if (canon_a and canon_b) else 0.0
    root_canon = SequenceMatcher(
        None, " ".join(root_a)[:500], " ".join(root_b)[:500]
    ).ratio() if (root_a and root_b) else 0.0

    if ta_exp and tb_exp:
        inter_e = len(ta_exp & tb_exp)
        jac_e = inter_e / len(ta_exp | tb_exp)
        cont_e = inter_e / min(len(ta_exp), len(tb_exp))
        inter_r = len(ta_raw & tb_raw)
        jac_r = inter_r / (len(ta_raw | tb_raw) or 1) if (ta_raw or tb_raw) else 0.0
        token_score = (
            0.25 * jac_e + 0.15 * cont_e + 0.15 * jac_r
            + 0.20 * root_jac + 0.15 * bag_jac + 0.10 * bag_cont
        )
        token_score *= min(1.0, (min(len(ta_raw), len(tb_raw)) or 1) / 2.0)
    else:
        token_score = 0.55 * root_jac + 0.45 * bag_jac

    # исходный порядок — слабый сигнал (не должен ломать перестановки)
    seq = SequenceMatcher(None, " ".join(words(a))[:700], " ".join(words(b))[:700]).ratio()
    order_free_seq = max(canon_seq, root_canon)

    # биграммы как множества пар (частично устойчивы к локальной перестановке)
    phrase_score = min(1.0, (len(pa & pb) / (len(pa | pb) or 1)) * 1.55) if (pa and pb) else 0.0
    ngram_score = (len(nga & ngb) / (len(nga | ngb) or 1)) if (nga and ngb) else 0.0

    if aa and ab:
        inter_a = len(aa & ab)
        soft = 0.0
        objs_a = {x[1] for x in aa}
        objs_b = {x[1] for x in ab}
        acts_a = {x[0] for x in aa}
        acts_b = {x[0] for x in ab}
        # объекты/действия как множества — порядок слов не важен
        soft += 0.5 * (len(objs_a & objs_b) / (len(objs_a | objs_b) or 1))
        soft += 0.35 * (len(acts_a & acts_b) / (len(acts_a | acts_b) or 1))
        for x in aa:
            for y in ab:
                if x[1] == y[1]:
                    soft += 0.25
                elif x[0] == y[0]:
                    soft += 0.1
        action_score = min(1.0, inter_a * 0.7 + soft * 0.2)
    else:
        action_score = 0.0

    score = (
        0.28 * concept_score
        + 0.24 * token_score
        + 0.04 * seq                 # почти не смотрим на исходный порядок
        + 0.12 * order_free_seq      # смотрим на отсортированный «канон»
        + 0.10 * phrase_score
        + 0.08 * ngram_score
        + 0.14 * action_score
    )

    # если мешки корней сильно совпали — это та же суть при любой перестановке
    if root_jac >= 0.55 or root_cont >= 0.7:
        score = max(score, 0.58 + 0.35 * root_jac)
    if bag_jac >= 0.55:
        score = max(score, 0.55 + 0.35 * bag_jac)
    if order_free_seq >= 0.75 and root_jac >= 0.4:
        score = max(score, 0.72)

    if concept_score >= 0.40 and (ca & cb):
        score = max(score, 0.46 + 0.42 * concept_score)
    if concept_score >= 0.55 and token_score >= 0.16:
        score = max(score, 0.63)
    if concept_score >= 0.72:
        score = max(score, 0.74)
    if concept_score >= 0.88:
        score = max(score, 0.84)
    if action_score >= 0.55 and concept_score >= 0.35:
        score = max(score, 0.67)
    if ngram_score >= 0.32 and token_score >= 0.22:
        score = min(1.0, score + 0.04)

    return max(0.0, min(1.0, score))

def find_similar(text, exclude_sid=None, limit=3, threshold=0.22):
    res = []
    with db() as con:
        for r in con.execute("SELECT id, text, status FROM ideas"):
            s = similarity(text, r["text"])
            if s >= threshold:
                res.append(("idea", r["id"], s, r["text"], r["status"]))
        for r in con.execute(
            "SELECT id, text, status FROM suggestions WHERE status!='accepted' AND id!=?",
            (exclude_sid or 0,),
        ):
            s = similarity(text, r["text"])
            if s >= threshold:
                res.append(("sug", r["id"], s, r["text"], r["status"]))
    res.sort(key=lambda x: x[2], reverse=True)
    return res[:limit]

def similar_block(similar):
    if not similar:
        return "🔍 Похожих идей в списке не найдено ✅"
    lines = ["🔍 <b>Похоже на уже добавленное:</b>"]
    for kind, _id, score, text, status in similar:
        pct = int(score * 100)
        if score >= 0.70:
            level = "🔴 почти то же самое"
        elif score >= 0.55:
            level = "🟠 очень похожа"
        elif score >= 0.40:
            level = "🟡 похожа"
        elif score >= 0.20:
            level = "⚪ немного похожа"
        else:
            level = "⚪ слабо похожа"
        if kind == "idea":
            label = f"Идея #{_id} ({status_info(status)[0]} {'в списке' if status in ('new', 'pending', 'chosen') else 'отклонена'})"
        else:
            label = f"Предложение #{_id} ({SUG_STATUS[status][1].lower()})"
        if pct < 20:
            head = f"• <b>{label}</b> · {level}"
        else:
            head = f"• <b>{label}</b> · {pct}% · {level}"
        lines.append(f"{head}\n   «{esc(short(text, 80))}»")
    return "\n".join(lines)


# ───────────────────────── предложения и чаты ─────────────────────────

def get_suggestion(sid):
    with db() as con:
        return con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()


def log_chat(sid, sender, text):
    with db() as con:
        con.execute("INSERT INTO sug_messages(suggestion_id,sender,text,created_at) VALUES(?,?,?,?)",
                    (sid, sender, text, now()))


def msg_text(message):
    return (message.text or message.caption or f"[{getattr(message.content_type, 'value', message.content_type)}]")[:4000]


def history_text(sid, admin, name=""):
    with db() as con:
        rows = con.execute("SELECT * FROM sug_messages WHERE suggestion_id=? ORDER BY id DESC LIMIT 12", (sid,)).fetchall()
    blocks, used, hidden = [], 0, False
    for r in rows:
        if r["sender"] == "user":
            who = f"🧑 <b>{esc(short(name, 20))}</b>" if admin else "🧑 <b>Вы</b>"
        else:
            who = "🤖 <b>Вы (от имени бота)</b>" if admin else "👑 <b>Создатель</b>"
        body = esc_cut(r["text"], 700)[0]
        block = f"{who} · <i>{fmt_dt(r['created_at'])}</i>\n{body}"
        if used + len(block) > 2800 and blocks:
            hidden = True
            break
        blocks.append(block)
        used += len(block) + 2
    text = "\n\n".join(reversed(blocks)) or "<i>Сообщений пока нет</i>"
    return ("<i>… ранние сообщения скрыты</i>\n\n" + text) if hidden or len(rows) == 12 else text


def sug_admin_kb(sug, in_chat=False):
    sid, st = sug["id"], sug["status"]
    rows = []
    if not in_chat:
        rows.append([B("💬 Открыть чат", f"sug:chat:{sid}")])
    if st == "open":
        rows.append([B("✅ В список идей", f"sug:acc:{sid}"), B("❌ Отклонить", f"sug:rej:{sid}")])
    elif st == "accepted":
        rows.append([B(f"✅ Принята · идея #{sug['idea_id']}", "noop")])
    else:
        rows.append([B("❌ Отклонена", "noop"), B("✅ Всё же принять", f"sug:acc:{sid}")])
    if in_chat:
        rows.append([B("🚪 Выйти из чата", "sug:exit"), B("⬅️ К предложениям", "adm:sug")])
    return KB(*rows)


def chat_view(sug, admin):
    emoji, label = SUG_STATUS[sug["status"]]
    head = f"💬 <b>Чат по идее #{sug['id']}</b> · {emoji} {label}"
    if admin:
        head += f"\n👤 {user_link(sug['user_id'], sug['full_name'], sug['username'])}"
    hist = history_text(sug["id"], admin, sug["full_name"])
    if admin:
        foot = "✍️ <i>Пиши сообщение — оно уйдёт автору от имени бота.</i>"
        kb = sug_admin_kb(sug, in_chat=True)
    else:
        foot = "✍️ <i>Пиши дополнения или вопросы — я передам их создателю, а его ответы придут сюда.</i>"
        kb = KB([B("🚪 Выйти из чата", "sug:exit")], [B("⬅️ К моим идеям", "idea:mine")])
    return f"{head}\n\n{hist}\n\n{foot}", kb


async def deliver(chat_id, header, message, markup=None):
    """Отправляет текст с заголовком или заголовок + копию вложения."""
    if message.text:
        body = esc_cut(message.text, 3700)[0]
        await bot.send_message(chat_id, f"{header}\n\n{body}", reply_markup=markup)
    else:
        await bot.send_message(chat_id, header, reply_markup=markup)
        await bot.copy_message(chat_id, message.chat.id, message.message_id)


# ───────────────────────── экраны ─────────────────────────

def admin_counts():
    with db() as con:
        s = con.execute("SELECT COUNT(*) n FROM suggestions WHERE status='open'").fetchone()["n"]
        r = con.execute("SELECT COUNT(*) n FROM requests WHERE status='pending'").fetchone()["n"]
        m = con.execute("SELECT COUNT(*) n FROM requests WHERE status='approved'").fetchone()["n"]
    return s, r, m


def screen_hub(user):
    text = (f"🎨 <b>Decide Visuals</b>\n\nПривет, {esc(user.first_name or user.full_name)}! 👋\n\n"
            f"Здесь можно скачать клиент и предложить свою идею — она сразу придёт создателю.\n\n"
            f"Выбери раздел:")
    rows = [[B("⬇️ Скачать клиент", "m:dl")], [B("💡 Идеи", "m:ideas")]]
    if is_admin(user.id):
        s, r, _ = admin_counts()
        badge = f" · {s + r}" if s + r else ""
        rows.append([B(f"🛠 Админ-панель{badge}", "m:admin")])
    return text, KB(*rows)


def screen_downloads(uid):
    lines = ["⬇️ <b>Скачать клиент</b>", ""]
    for kind, (emoji, title, _) in KINDS.items():
        f = get_file(kind)
        if f:
            lines.append(f"{emoji} <b>{title}</b>\n└ {esc(f['file_name'])} · {fmt_size(f['file_size'])} · от {fmt_date(f['uploaded_at'])}")
            if f["caption"]:
                lines.append(f"└ 📝 {esc_cut(f['caption'], 300)[0]}")
        else:
            lines.append(f"{emoji} <b>{title}</b>\n└ <i>пока не загружен</i>")
        lines.append("")
    lines.append("Выбери, что скачать 👇")
    rows = [[B("📦 Скачать мод", "dl:mod"), B("🚀 Скачать лаунчер", "dl:launcher")]]
    if is_admin(uid):
        rows.append([B("⬆️ Загрузить мод", "up:mod")])
        rows.append([B("⬆️ Загрузить лаунчер", "up:launcher")])
    rows.append([B("⬅️ Назад", "m:home")])
    return "\n".join(lines), KB(*rows)


def screen_ideas(uid):
    st = access(uid)
    with db() as con:
        n = con.execute("SELECT COUNT(*) n FROM suggestions WHERE user_id=?", (uid,)).fetchone()["n"]
    text = ("💡 <b>Идеи</b>\n\nПредложи свою идею для проекта — она сразу уйдёт создателю, "
            "а обсудить её можно будет прямо здесь, в чате.")
    if st == "approved":
        text += "\n\n📚 У тебя есть доступ к общему списку идей."
        access_btn = B("📚 Открыть идеи", "v:open")
    elif st == "pending":
        text += "\n\n⏳ Заявка на просмотр идей на рассмотрении."
        access_btn = B("⏳ Заявка на рассмотрении", "noop:pending")
    elif st == "rejected":
        text += "\n\n⚠️ Прошлая заявка на просмотр идей отклонена — можно подать новую."
        access_btn = B("🔑 Подать заявку снова", "acc:req")
    else:
        text += "\n\n🔒 Общий список идей открывается по заявке — её рассматривает создатель."
        access_btn = B("🔑 Заявка на просмотр идей", "acc:req")
    rows = [[B("✍️ Предложить идею", "idea:suggest")]]
    if n:
        rows.append([B(f"📂 Мои идеи ({n})", "idea:mine")])
    rows.append([access_btn])
    rows.append([B("⬅️ Назад", "m:home")])
    return text, KB(*rows)


def screen_my_ideas(uid):
    with db() as con:
        rows = con.execute("SELECT * FROM suggestions WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)).fetchall()
    text = "📂 <b>Мои идеи</b>\n\nВыбери идею, чтобы открыть её чат — там можно дополнить её или задать вопрос."
    if not rows:
        text = "📂 <b>Мои идеи</b>\n\nТы пока ничего не предлагал(а)."
    btns = [[B(f"{SUG_STATUS[r['status']][0]} #{r['id']} · {short(r['text'], 30)}", f"my:open:{r['id']}")] for r in rows]
    btns.append([B("✍️ Предложить ещё", "idea:suggest")])
    btns.append([B("⬅️ Назад", "m:ideas")])
    return text, KB(*btns)


def screen_admin():
    s, r, m = admin_counts()
    text = "🛠 <b>Админ-панель</b>\n\nВыбери раздел:"
    return text, KB(
        [B(f"📥 Предложения · {s}" if s else "📥 Предложения", "adm:sug")],
        [B(f"👥 Заявки · {r}" if r else "👥 Заявки", "adm:req"), B(f"🧑‍💻 Участники · {m}", "adm:mem")],
        [B("🗂 Все идеи", "adm:all"), B("➕ Добавить идеи", "adm:add")],
        [B("📊 Статистика", "adm:stat"), B("🛡 Админы", "adm:admins")],
        [B("⬅️ Назад", "m:home")],
    )


def screen_suggestions(show_all):
    with db() as con:
        q = "SELECT * FROM suggestions" + ("" if show_all else " WHERE status='open'") + " ORDER BY id DESC LIMIT 20"
        rows = con.execute(q).fetchall()
    if show_all:
        text = "📥 <b>Все предложения</b> (последние 20)"
    else:
        text = f"📥 <b>Предложения на рассмотрении</b>: {len(rows)}" if rows else "📥 <b>Предложения</b>\n\nНовых предложений нет 🎉"
    btns = [[B(f"{SUG_STATUS[r['status']][0]} #{r['id']} · {short(r['full_name'], 12)} · {short(r['text'], 24)}",
               f"sug:open:{r['id']}")] for r in rows]
    btns.append([B("🕓 Только новые" if show_all else "🗄 Показать все", "adm:sug" if show_all else "adm:sug:all")])
    btns.append([B("⬅️ Назад", "m:admin")])
    return text, KB(*btns)


def screen_requests():
    with db() as con:
        rows = con.execute("SELECT * FROM requests WHERE status='pending' ORDER BY created_at").fetchall()
    if not rows:
        return "👥 <b>Заявки на просмотр идей</b>\n\nНовых заявок нет.", KB([B("⬅️ Назад", "m:admin")])
    lines = [f"👥 <b>Заявки на просмотр идей</b>: {len(rows)}", ""]
    btns = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {user_link(r['user_id'], r['full_name'], r['username'])}")
        btns.append([B(f"✅ {short(r['full_name'], 16)}", f"rq:yes:{r['user_id']}"), B("❌ Отклонить", f"rq:no:{r['user_id']}")])
    btns.append([B("⬅️ Назад", "m:admin")])
    return "\n".join(lines), KB(*btns)


def screen_members():
    with db() as con:
        rows = con.execute("SELECT * FROM requests WHERE status='approved' ORDER BY created_at").fetchall()
    if not rows:
        return "🧑‍💻 <b>Участники</b>\n\nУчастников с доступом пока нет.", KB([B("⬅️ Назад", "m:admin")])
    lines = [f"🧑‍💻 <b>Участники с доступом</b>: {len(rows)}", ""]
    btns = []
    for r in rows:
        lines.append(f"• {user_link(r['user_id'], r['full_name'], r['username'])}")
        btns.append([B(f"🚫 Отозвать: {short(r['full_name'], 20)}", f"rq:revoke:{r['user_id']}")])
    btns.append([B("⬅️ Назад", "m:admin")])
    return "\n".join(lines), KB(*btns)


def screen_admins():
    with db() as con:
        extra = con.execute("SELECT * FROM admins ORDER BY added_at").fetchall()
        known = {r["user_id"]: r for r in con.execute("SELECT * FROM users")}
    lines = ["🛡 <b>Администраторы</b>", "", "Полный доступ к боту — такой же, как у владельца.", ""]
    for oid in sorted(ADMIN_IDS):
        u = known.get(oid)
        lines.append(f"👑 {user_link(oid, u['full_name'] if u else oid, u['username'] if u else None)} — владелец")
    btns = []
    for r in extra:
        lines.append(f"🛡 {user_link(r['user_id'], r['full_name'], r['username'])}")
        btns.append([B(f"🗑 Снять: {short(r['full_name'] or r['username'], 20)}", f"adm:rmadm:{r['user_id']}")])
    btns.insert(0, [B("➕ Выдать полный доступ", "adm:addadm")])
    btns.append([B("⬅️ Назад", "m:admin")])
    return "\n".join(lines), KB(*btns)


def screen_stats():
    c = counts_ideas()
    with db() as con:
        total_users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        sug_open = con.execute("SELECT COUNT(*) n FROM suggestions WHERE status='open'").fetchone()["n"]
        sug_all = con.execute("SELECT COUNT(*) n FROM suggestions").fetchone()["n"]
        logs = con.execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
    s, r, m = admin_counts()
    files = " · ".join(f"{KINDS[k][1]} {'✅' if get_file(k) else '—'}" for k in KINDS)
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"💡 <b>Идеи</b>: всего {sum(c.values())}\n"
        f"🟢 Не трогали: {c.get('new', 0)}\n"
        f"🟢 Выбраны, ждут списка: {c.get('pending', 0)}\n"
        f"🟡 Зафиксированы отчётом: {c.get('chosen', 0)}\n"
        f"🔴 Отклонены: {c.get('declined', 0)}\n\n"
        f"📥 <b>Предложения</b>: на рассмотрении {sug_open} из {sug_all}\n"
        f"👥 <b>Люди</b>: пользователей {total_users} · с доступом {m} · заявок {r}\n"
        f"📦 <b>Файлы</b>: {files}\n"
        f"🧾 Записей истории: {logs}"
    )
    return text, KB([B("⬅️ Назад", "m:admin")])


# ───────────────────────── команды и главное меню ─────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    clear_session(message.from_user.id)
    text, kb = screen_hub(message.from_user)
    await message.answer(text, reply_markup=kb)


@dp.message(Command("menu"))
@dp.message(F.text == MENU_BTN)
async def cmd_menu(message: Message):
    clear_session(message.from_user.id)
    text, kb = screen_hub(message.from_user)
    await message.answer(text, reply_markup=kb)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    clear_session(message.from_user.id)
    await message.answer("Ок, отменено.")
    text, kb = screen_hub(message.from_user)
    await message.answer(text, reply_markup=kb)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@dp.callback_query(F.data.startswith("noop"))
async def cb_noop(call: CallbackQuery):
    if call.data == "noop:pending":
        await ack(call, "Заявка рассматривается — дождись решения 🙂", alert=True)
    else:
        await ack(call)


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(call: CallbackQuery):
    uid = call.from_user.id
    clear_session(uid)
    where = call.data[2:]
    if where == "home":
        text, kb = screen_hub(call.from_user)
    elif where == "dl":
        text, kb = screen_downloads(uid)
    elif where == "ideas":
        text, kb = screen_ideas(uid)
    elif where == "admin":
        if not await need_admin(call):
            return
        text, kb = screen_admin()
    else:
        await ack(call)
        return
    await edit(call, text, kb)
    await ack(call)


# ───────────────────────── скачивание и загрузка клиента ─────────────────────────

@dp.callback_query(F.data.startswith("dl:"))
async def cb_download(call: CallbackQuery):
    kind = call.data.split(":")[1]
    if kind not in KINDS:
        await ack(call)
        return
    emoji, title, _ = KINDS[kind]
    f = get_file(kind)
    if not f:
        await ack(call, f"{title} пока не загружен 😔", alert=True)
        return
    cap = f"{emoji} <b>{title}</b>\n{esc(f['file_name'])} · {fmt_size(f['file_size'])}"
    if f["caption"]:
        cap += f"\n\n{esc_cut(f['caption'], 600)[0]}"
    try:
        await bot.send_document(call.from_user.id, f["file_id"], caption=cap)
        await ack(call, "Отправляю файл…")
    except TelegramAPIError:
        logging.exception("Не удалось отправить файл")
        await ack(call, "Не удалось отправить файл. Сообщи создателю.", alert=True)


@dp.callback_query(F.data.in_({"up:mod", "up:launcher"}))
async def cb_upload_begin(call: CallbackQuery):
    if not await need_admin(call):
        return
    kind = call.data.split(":")[1]
    emoji, title, gen = KINDS[kind]
    set_session(call.from_user.id, f"upload_{kind}")
    await edit(
        call,
        f"⬆️ <b>Загрузка {gen}</b>\n\nОтправь файл {gen} сообщением (как документ 📎).\n"
        f"Подпись к файлу станет описанием, например версия.",
        KB([B("❌ Отмена", "up:cancel")]),
    )
    await ack(call)


@dp.callback_query(F.data == "up:cancel")
async def cb_upload_cancel(call: CallbackQuery):
    clear_session(call.from_user.id)
    text, kb = screen_downloads(call.from_user.id)
    await edit(call, text, kb)
    await ack(call, "Отменено")


async def handle_upload_file(message: Message, kind):
    emoji, title, gen = KINDS[kind]
    doc = message.document
    if not doc:
        await message.answer(f"Отправь именно <b>файл</b> {gen} (📎 как документ), а не фото или текст.",
                             reply_markup=KB([B("❌ Отмена", "up:cancel")]))
        return
    info = {
        "kind": kind, "file_id": doc.file_id, "unique": doc.file_unique_id,
        "name": doc.file_name or "file", "size": doc.file_size or 0,
        "caption": (message.caption or "").strip()[:500],
    }
    uid = message.from_user.id
    old = get_file(kind)
    if old and old["file_unique_id"] == info["unique"]:
        clear_session(uid)
        await message.answer(f"ℹ️ Этот файл уже загружен как {gen}.", reply_markup=KB([B("⬇️ К загрузкам", "m:dl")]))
        return
    if old:
        set_session(uid, "file_confirm", None, info)
        await message.answer(
            f"⚠️ <b>{title} уже загружен</b>\n\n"
            f"Сейчас: {esc(old['file_name'])} · {fmt_size(old['file_size'])} · от {fmt_date(old['uploaded_at'])}\n"
            f"Новый: {esc(info['name'])} · {fmt_size(info['size'])}\n\nЗаменить файл?",
            reply_markup=KB([B("✅ Подтвердить замену", "rp:yes"), B("❌ Отмена", "rp:no")]),
        )
        return
    save_file(kind, info, uid)
    clear_session(uid)
    await message.answer(f"✅ <b>{title} загружен!</b>\n{esc(info['name'])} · {fmt_size(info['size'])}",
                         reply_markup=KB([B("⬇️ К загрузкам", "m:dl")]))


@dp.callback_query(F.data.in_({"rp:yes", "rp:no"}))
async def cb_replace(call: CallbackQuery):
    if not await need_admin(call):
        return
    uid = call.from_user.id
    sess = get_session(uid)
    if not sess or sess["mode"] != "file_confirm":
        await edit(call, "Это подтверждение уже неактуально.", KB([B("⬇️ К загрузкам", "m:dl")]))
        await ack(call)
        return
    clear_session(uid)
    if call.data == "rp:no":
        await edit(call, "Замена отменена — старый файл остался.", KB([B("⬇️ К загрузкам", "m:dl")]))
        await ack(call)
        return
    info = sess["data"]
    save_file(info["kind"], info, uid)
    title = KINDS[info["kind"]][1]
    await edit(call, f"✅ <b>{title} заменён!</b>\n{esc(info['name'])} · {fmt_size(info['size'])}",
               KB([B("⬇️ К загрузкам", "m:dl")]))
    await ack(call, "Готово")


# ───────────────────────── заявки на просмотр идей ─────────────────────────

@dp.callback_query(F.data == "acc:req")
async def cb_request_access(call: CallbackQuery):
    u = call.from_user
    st = access(u.id)
    if st == "approved":
        await ack(call, "Доступ уже есть 🙂", alert=True)
        return
    if st == "pending":
        await ack(call, "Заявка уже отправлена — дождись решения.", alert=True)
        return
    with db() as con:
        con.execute("INSERT OR REPLACE INTO requests(user_id,username,full_name,status,created_at) VALUES(?,?,?,?,?)",
                    (u.id, u.username, u.full_name, "pending", now()))
    text, kb = screen_ideas(u.id)
    await edit(call, text, kb)
    await ack(call, "Заявка отправлена создателю ✅", alert=True)
    for aid in all_admin_ids():
        try:
            await bot.send_message(
                aid,
                f"🔑 <b>Заявка на просмотр идей</b>\n\n{user_link(u.id, u.full_name, u.username)}\nID: <code>{u.id}</code>",
                reply_markup=KB([B("✅ Одобрить", f"rqn:yes:{u.id}"), B("❌ Отклонить", f"rqn:no:{u.id}")]),
            )
        except TelegramAPIError:
            pass


async def apply_decision(action, uid):
    """-> (успех, текст результата)"""
    with db() as con:
        row = con.execute("SELECT status FROM requests WHERE user_id=?", (uid,)).fetchone()
        st = row["status"] if row else None
        if action in ("yes", "no"):
            if st != "pending":
                return False, "Заявка уже обработана"
            con.execute("UPDATE requests SET status=? WHERE user_id=?", ("approved" if action == "yes" else "rejected", uid))
        elif action == "revoke":
            if st != "approved":
                return False, "У пользователя нет доступа"
            con.execute("UPDATE requests SET status='rejected' WHERE user_id=?", (uid,))
    try:
        if action == "yes":
            await bot.send_message(uid, "✅ <b>Доступ к просмотру идей одобрен!</b>",
                                   reply_markup=KB([B("📚 Открыть идеи", "v:open")]))
        elif action == "no":
            await bot.send_message(uid, "❌ Заявка на просмотр идей отклонена.")
        else:
            await bot.send_message(uid, "🚫 Доступ к просмотру идей закрыт.")
    except TelegramAPIError:
        pass
    return True, {"yes": "✅ Одобрено", "no": "❌ Отклонено", "revoke": "🚫 Доступ отозван"}[action]


@dp.callback_query(F.data.startswith("rqn:"))
async def cb_request_notice(call: CallbackQuery):
    if not await need_admin(call):
        return
    _, action, raw = call.data.split(":")
    ok, result = await apply_decision(action, int(raw))
    await edit(call, f"{call.message.html_text}\n\n<b>{result}</b>")
    await ack(call, result)


@dp.callback_query(F.data.startswith("rq:"))
async def cb_request_list(call: CallbackQuery):
    if not await need_admin(call):
        return
    _, action, raw = call.data.split(":")
    ok, result = await apply_decision(action, int(raw))
    text, kb = screen_members() if action == "revoke" else screen_requests()
    await edit(call, text, kb)
    await ack(call, result)


# ───────────────────────── предложение идеи и чаты ─────────────────────────

@dp.callback_query(F.data == "idea:suggest")
async def cb_suggest(call: CallbackQuery):
    set_session(call.from_user.id, "new_idea")
    await edit(
        call,
        "✍️ <b>Предложить идею</b>\n\nНапиши свою идею одним сообщением — как можно подробнее. "
        "Я передам её создателю, а здесь откроется чат по этой идее.",
        KB([B("❌ Отмена", "idea:cancel")]),
    )
    await ack(call)


@dp.callback_query(F.data == "idea:cancel")
async def cb_suggest_cancel(call: CallbackQuery):
    clear_session(call.from_user.id)
    text, kb = screen_ideas(call.from_user.id)
    await edit(call, text, kb)
    await ack(call, "Отменено")


@dp.callback_query(F.data == "idea:mine")
async def cb_my_ideas(call: CallbackQuery):
    clear_session(call.from_user.id)
    text, kb = screen_my_ideas(call.from_user.id)
    await edit(call, text, kb)
    await ack(call)


async def handle_new_idea(message: Message):
    text = (message.text or "").strip()
    cancel = KB([B("❌ Отмена", "idea:cancel")])
    if not text:
        await message.answer("Напиши идею <b>текстом</b> ✍️\n<i>Вложения можно будет отправить позже — в чате идеи.</i>",
                             reply_markup=cancel)
        return
    if len(text) < 10:
        await message.answer("Слишком коротко 🙂 Опиши идею чуть подробнее.", reply_markup=cancel)
        return
    if len(text) > 3500:
        await message.answer(f"Слишком длинно ({len(text)} символов). Сократи до 3500 — детали можно дописать в чате.",
                             reply_markup=cancel)
        return
    u = message.from_user
    with db() as con:
        cur = con.execute(
            "INSERT INTO suggestions(user_id,username,full_name,text,status,created_at) VALUES(?,?,?,?,?,?)",
            (u.id, u.username, u.full_name, text, "open", now()),
        )
        sid = cur.lastrowid
    log_chat(sid, "user", text)
    similar = find_similar(text, exclude_sid=sid)
    set_session(u.id, "chat", sid)
    await message.answer(
        f"✅ <b>Идея #{sid} отправлена создателю!</b>\n\n"
        "💬 Это чат по вашей идее. Пиши сюда дополнения — они придут создателю, а его ответы появятся здесь.\n\n"
        "<i>Выйти из чата можно кнопкой ниже. Вернуться — через «Идеи → Мои идеи».</i>",
        reply_markup=KB([B("🚪 Выйти из чата", "sug:exit")], [B("📂 Мои идеи", "idea:mine")]),
    )
    sug = get_suggestion(sid)
    body = esc_cut(text, 2500)[0]
    notice = (f"💡 <b>Новая идея #{sid}</b>\nот {user_link(u.id, u.full_name, u.username)}\n\n{body}\n\n"
              f"{similar_block(similar)}")
    for aid in all_admin_ids():
        try:
            await bot.send_message(aid, notice, reply_markup=sug_admin_kb(sug))
        except TelegramAPIError:
            pass


async def handle_user_chat(message: Message, sid):
    u = message.from_user
    sug = get_suggestion(sid)
    if not sug or sug["user_id"] != u.id:
        clear_session(u.id)
        await message.answer("Этот чат недоступен. Открой «🏠 Главное меню».")
        return
    log_chat(sid, "user", msg_text(message))
    header = f"💬 <b>Идея #{sid}</b> · {user_link(u.id, u.full_name, u.username)}"
    sent = False
    for aid in all_admin_ids():
        try:
            await deliver(aid, header, message, KB([B("💬 Ответить", f"sug:chat:{sid}")]))
            sent = True
        except TelegramAPIError:
            pass
    await message.answer("✅ Передано создателю" if sent else "⚠️ Не удалось передать сообщение, попробуй позже.")


async def handle_admin_chat(message: Message, sid):
    sug = get_suggestion(sid)
    if not sug:
        clear_session(message.from_user.id)
        await message.answer("Предложение не найдено.")
        return
    header = f"👑 <b>Создатель</b> · идея #{sid}"
    try:
        await deliver(sug["user_id"], header, message, KB([B("✍️ Ответить", f"my:reply:{sid}")]))
    except TelegramForbiddenError:
        await message.answer("⚠️ Не доставлено: пользователь заблокировал бота.")
        return
    except TelegramAPIError:
        logging.exception("Ошибка доставки")
        await message.answer("⚠️ Не удалось доставить сообщение.")
        return
    log_chat(sid, "admin", msg_text(message))
    await message.answer(f"✅ Отправлено · идея #{sid}")


@dp.callback_query(F.data.startswith("my:"))
async def cb_my_open(call: CallbackQuery):
    _, mode, raw = call.data.split(":")
    sid = int(raw)
    sug = get_suggestion(sid)
    if not sug or sug["user_id"] != call.from_user.id:
        await ack(call, "Нет доступа", alert=True)
        return
    set_session(call.from_user.id, "chat", sid)
    text, kb = chat_view(sug, admin=False)
    if mode == "reply":
        await call.message.answer(text, reply_markup=kb)
    else:
        await edit(call, text, kb)
    await ack(call)


@dp.callback_query(F.data == "sug:exit")
async def cb_chat_exit(call: CallbackQuery):
    clear_session(call.from_user.id)
    if is_admin(call.from_user.id):
        text, kb = screen_admin()
    else:
        text, kb = screen_ideas(call.from_user.id)
    await edit(call, text, kb)
    await ack(call, "Вышел из чата")


@dp.callback_query(F.data.startswith("sug:"))
async def cb_suggestion(call: CallbackQuery):
    if not await need_admin(call):
        return
    parts = call.data.split(":")
    action, sid = parts[1], int(parts[2])
    sug = get_suggestion(sid)
    if not sug:
        await ack(call, "Предложение не найдено", alert=True)
        return

    if action in ("open", "chat"):
        set_session(call.from_user.id, "admin_chat", sid)
        text, kb = chat_view(sug, admin=True)
        if action == "chat":
            await call.message.answer(text, reply_markup=kb)
        else:
            await edit(call, text, kb)
        await ack(call)
        return

    in_chat = False
    if call.message.reply_markup:
        in_chat = any(b.callback_data == "sug:exit" for row in call.message.reply_markup.inline_keyboard for b in row)

    if action == "acc":
        if sug["status"] == "accepted":
            await ack(call, "Уже принята", alert=True)
            return
        with db() as con:
            cur = con.execute(
                "INSERT INTO ideas(text,status,created_at,created_by,source_suggestion) VALUES(?,?,?,?,?)",
                (sug["text"], "new", now(), sug["user_id"], sid),
            )
            idea_id = cur.lastrowid
            con.execute("UPDATE suggestions SET status='accepted', idea_id=? WHERE id=?", (idea_id, sid))
        try:
            await bot.send_message(
                sug["user_id"],
                f"🎉 <b>Твоя идея #{sid} принята в список идей!</b>\n\nСпасибо за вклад в проект 💚",
                reply_markup=KB([B("💬 Открыть чат", f"my:reply:{sid}")]),
            )
        except TelegramAPIError:
            pass
        toast = f"Добавлено в список · идея #{idea_id}"
    elif action == "rej":
        if sug["status"] != "open":
            await ack(call, "Предложение уже обработано", alert=True)
            return
        with db() as con:
            con.execute("UPDATE suggestions SET status='rejected' WHERE id=?", (sid,))
        try:
            await bot.send_message(
                sug["user_id"],
                f"❌ <b>Идея #{sid} не вошла в список.</b>\n\nЕсли есть вопросы — напиши в чате идеи.",
                reply_markup=KB([B("💬 Открыть чат", f"my:reply:{sid}")]),
            )
        except TelegramAPIError:
            pass
        toast = "Отклонено"
    else:
        await ack(call)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=sug_admin_kb(get_suggestion(sid), in_chat=in_chat))
    except TelegramBadRequest:
        pass
    await ack(call, toast)


# ───────────────────────── просмотр идей ─────────────────────────

@dp.callback_query(F.data == "v:open")
async def cb_viewer_open(call: CallbackQuery):
    if not await need_access(call):
        return
    clear_session(call.from_user.id)
    await show_idea(call, "all", 0, exact=False)
    await ack(call)


@dp.callback_query(F.data.startswith("i:"))
async def cb_nav(call: CallbackQuery):
    if not await need_access(call):
        return
    f, iid = parse_fi(call.data)
    await show_idea(call, f, iid)
    await ack(call)


@dp.callback_query(F.data.startswith("vf:"))
async def cb_filter(call: CallbackQuery):
    if not await need_access(call):
        return
    f, iid = parse_fi(call.data)
    await show_idea(call, f, iid, exact=False)
    await ack(call)


@dp.callback_query(F.data.startswith("full:"))
async def cb_full(call: CallbackQuery):
    if not await need_access(call):
        return
    idea = get_idea(int(call.data.split(":")[1]))
    if not idea:
        await ack(call, "Идея не найдена", alert=True)
        return
    lines = [f"📄 <b>Идея #{idea['id']}</b>", ""] + esc_pieces(idea["text"])
    for chunk in pack(lines):
        await call.message.answer(chunk)
    await ack(call)


@dp.callback_query(F.data.startswith(("pick:", "decl:", "ret:")))
async def cb_change_status(call: CallbackQuery):
    if not await need_access(call):
        return
    action = call.data.split(":")[0]
    f, iid = parse_fi(call.data)
    idea = get_idea(iid)
    if not idea:
        await ack(call, "Идея не найдена (возможно, удалена)", alert=True)
        await show_idea(call, f, iid, exact=False)
        return
    st = idea["status"]
    alert = toast = None
    new_status = None
    if action == "pick":
        if st == "declined":
            alert = "Идея отклонена — сначала верни её в список."
        elif st in ("pending", "chosen"):
            alert = "Идея уже взята."
        else:
            new_status, toast = "pending", "Добавлено в общий список"
    elif action == "decl":
        if st == "chosen":
            alert = "Идея зафиксирована отчётом — сначала верни её в список."
        elif st == "declined":
            alert = "Идея уже отклонена."
        else:
            new_status, toast = "declined", "Идея отклонена"
    else:  # ret
        if st == "new":
            alert = "Идея и так в списке."
        else:
            new_status, toast = "new", "Идея возвращена в список"
    if new_status:
        set_status(iid, new_status, call.from_user)
    await show_idea(call, f, iid)
    await ack(call, alert or toast, alert=bool(alert))


@dp.callback_query(F.data.startswith("rpt:"))
async def cb_report(call: CallbackQuery):
    if not await need_access(call):
        return
    f, iid = parse_fi(call.data)

    # В отчёт входят только выбранные идеи (pending/chosen).
    with db() as con:
        selected = con.execute(
            "SELECT * FROM ideas WHERE status IN ('pending', 'chosen') ORDER BY id"
        ).fetchall()

        if not selected:
            await ack(call, "Сначала выбери хотя бы одну идею.", alert=True)
            await show_idea(call, f, iid, exact=False)
            return

        new_ids = [r["id"] for r in selected if r["status"] == "pending"]
        con.execute(
            "UPDATE ideas SET status='chosen' WHERE status='pending'"
        )
        for idea_id in new_ids:
            con.execute(
                "INSERT INTO actions(idea_id,user_id,action,created_at) VALUES(?,?,?,?)",
                (idea_id, call.from_user.id, "included_in_report", now()),
            )

    lines = [f"📋 <b>ВЫБРАННЫЕ ИДЕИ — {len(selected)}</b>", ""]
    for idea in selected:
        parts = esc_pieces(idea["text"])
        parts[0] = f"🟡 <b>#{idea['id']}</b> — {parts[0]}"
        if idea["status_by_name"]:
            parts[-1] += f"  <i>— {esc(short(idea['status_by_name'], 30))}</i>"
        if idea["id"] in new_ids:
            parts[-1] += " 🆕"
        lines.extend(parts)

    for chunk in pack(lines):
        await call.message.answer(chunk)

    await show_idea(call, f, iid, exact=False)
    await ack(call, "Отправлены только выбранные идеи ✅")

@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit_begin(call: CallbackQuery):
    if not await need_admin(call):
        return
    iid = int(call.data.split(":")[1])
    idea = get_idea(iid)
    if not idea:
        await ack(call, "Идея не найдена", alert=True)
        return
    set_session(call.from_user.id, "edit_idea", iid)
    await edit(call, f"✏️ <b>Изменение идеи #{iid}</b>\n\nТекущий текст:\n{esc_cut(idea['text'], 3000)[0]}\n\n"
                     f"Отправь новый текст одним сообщением.", KB([B("❌ Отмена", f"ed:no:{iid}")]))
    await ack(call)


@dp.callback_query(F.data.startswith("ed:no:"))
async def cb_edit_cancel(call: CallbackQuery):
    clear_session(call.from_user.id)
    await show_idea(call, "all", int(call.data.split(":")[2]), exact=False)
    await ack(call, "Отменено")


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_ask(call: CallbackQuery):
    if not await need_admin(call):
        return
    f, iid = parse_fi(call.data)
    idea = get_idea(iid)
    if not idea:
        await ack(call, "Идея не найдена", alert=True)
        return
    await edit(
        call,
        f"🗑 <b>Удалить идею #{iid}?</b>\n\n{esc_cut(idea['text'], 1500)[0]}\n\n<i>Это действие нельзя отменить.</i>",
        KB([B("🗑 Да, удалить", f"delyes:{f}:{iid}")], [B("⬅️ Нет, назад", f"i:{f}:{iid}")]),
    )
    await ack(call)


@dp.callback_query(F.data.startswith("delyes:"))
async def cb_delete_do(call: CallbackQuery):
    if not await need_admin(call):
        return
    f, iid = parse_fi(call.data)
    with db() as con:
        con.execute("DELETE FROM ideas WHERE id=?", (iid,))
        con.execute("UPDATE suggestions SET idea_id=NULL WHERE idea_id=?", (iid,))
    await show_idea(call, f, iid, exact=False)
    await ack(call, "Идея удалена")


# ───────────────────────── админ-панель ─────────────────────────

@dp.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: CallbackQuery):
    if not await need_admin(call):
        return
    uid = call.from_user.id
    clear_session(uid)
    parts = call.data.split(":")
    section = parts[1]
    toast = None
    if section == "sug":
        text, kb = screen_suggestions(len(parts) > 2 and parts[2] == "all")
    elif section == "req":
        text, kb = screen_requests()
    elif section == "mem":
        text, kb = screen_members()
    elif section == "stat":
        text, kb = screen_stats()
    elif section == "admins":
        text, kb = screen_admins()
    elif section == "addadm":
        set_session(uid, "add_admin")
        text = ("➕ <b>Выдать полный доступ</b>\n\nОтправь @username человека.\n\n"
                "<i>Человек должен хотя бы раз открыть бота и нажать «Запустить» — иначе я не смогу его найти.</i>")
        kb = KB([B("❌ Отмена", "adm:admins")])
    elif section == "addyes":
        target = int(parts[2])
        with db() as con:
            u = con.execute("SELECT * FROM users WHERE user_id=?", (target,)).fetchone()
        if not u:
            toast = "Пользователь не найден"
        elif is_admin(target):
            toast = "Он уже администратор"
        else:
            with db() as con:
                con.execute("INSERT OR REPLACE INTO admins(user_id,username,full_name,added_by,added_at) VALUES(?,?,?,?,?)",
                            (target, u["username"], u["full_name"], uid, now()))
            toast = "Доступ выдан ✅"
            try:
                await bot.send_message(
                    target,
                    "🛡 <b>Тебе выдан полный доступ администратора.</b>\n\nОткрой меню — там появилась «Админ-панель».",
                    reply_markup=KB([B("🏠 Открыть меню", "m:home")]),
                )
            except TelegramAPIError:
                pass
        text, kb = screen_admins()
    elif section == "rmadm":
        target = int(parts[2])
        if target in ADMIN_IDS:
            toast = "Владельца нельзя снять"
        elif target == uid:
            toast = "Себя убрать нельзя"
        else:
            with db() as con:
                removed = con.execute("DELETE FROM admins WHERE user_id=?", (target,)).rowcount
            if removed:
                clear_session(target)
                toast = "Доступ снят"
                try:
                    await bot.send_message(target, "Доступ администратора снят.")
                except TelegramAPIError:
                    pass
        text, kb = screen_admins()
    elif section == "add":
        set_session(uid, "upload_ideas")
        text = ("➕ <b>Добавить идеи</b>\n\nОтправь идеи текстом или TXT-файлом (UTF-8).\n\n"
                "• Нумеруй пункты: <code>1. …</code>, <code>2. …</code>\n"
                "• или разделяй идеи строкой <code>---</code>\n\nПеред сохранением покажу предпросмотр.")
        kb = KB([B("❌ Отмена", "m:admin")])
    elif section == "all":
        with db() as con:
            rows = con.execute("SELECT * FROM ideas ORDER BY id").fetchall()
        if not rows:
            await ack(call, "Идей пока нет", alert=True)
            return
        lines = [f"🗂 <b>Все идеи</b>: {len(rows)}", ""]
        for r in rows:
            emoji, label = status_info(r["status"])
            pcs = esc_pieces(r["text"])
            pcs[0] = f"{emoji} <b>#{r['id']}</b> — {pcs[0]}"
            pcs.append(f"<i>{esc(label)}</i>" + (f" · {esc(r['status_by_name'])}" if r["status_by_name"] and r["status"] != "new" else ""))
            pcs.append("")
            lines.extend(pcs)
        for chunk in pack(lines):
            await call.message.answer(chunk)
        await ack(call)
        return
    else:
        await ack(call)
        return
    await edit(call, text, kb)
    await ack(call, toast)


async def preview_upload(message: Message, raw):
    uid = message.from_user.id
    items = parse_ideas(raw)
    if not items:
        await message.answer("Идей не найдено. Проверь формат и попробуй ещё раз.", reply_markup=KB([B("❌ Отмена", "m:admin")]))
        return
    with db() as con:
        existing = {norm_key(r["text"]) for r in con.execute("SELECT text FROM ideas")}
    new_items = [i for i in items if norm_key(i) not in existing]
    skipped = len(items) - len(new_items)
    if not new_items:
        await message.answer(f"Все {len(items)} идей уже есть в списке — добавлять нечего.",
                             reply_markup=KB([B("⬅️ В админ-панель", "m:admin")]))
        clear_session(uid)
        return
    set_session(uid, "ideas_confirm", None, {"items": new_items})
    head = f"📝 <b>Найдено идей: {len(new_items)}</b>" + (f" · пропущено дублей: {skipped}" if skipped else "")
    lines = [head, ""]
    for i, item in enumerate(new_items, 1):
        pcs = esc_pieces(item)
        pcs[0] = f"<b>{i}.</b> {pcs[0]}"
        lines.extend(pcs)
        lines.append("")
    lines.append("Проверь разделение. Сохранить?")
    packs = pack(lines)
    for chunk in packs[:-1]:
        await message.answer(chunk)
    await message.answer(packs[-1], reply_markup=KB([B("✅ Сохранить", "upi:yes"), B("❌ Отмена", "upi:no")]))


async def handle_upload_ideas(message: Message):
    if message.document:
        doc = message.document
        if not (doc.file_name or "").lower().endswith(".txt"):
            await message.answer("Поддерживается только TXT-файл.")
            return
        if (doc.file_size or 0) > 2_000_000:
            await message.answer("Файл слишком большой (макс. 2 МБ).")
            return
        buf = BytesIO()
        await bot.download(doc, destination=buf)
        try:
            raw = buf.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.answer("Сохрани TXT в кодировке UTF-8 и загрузи снова.")
            return
    elif message.text:
        raw = message.text
    else:
        await message.answer("Отправь идеи текстом или TXT-файлом.")
        return
    await preview_upload(message, raw)


@dp.callback_query(F.data.in_({"upi:yes", "upi:no"}))
async def cb_upload_ideas_confirm(call: CallbackQuery):
    if not await need_admin(call):
        return
    uid = call.from_user.id
    sess = get_session(uid)
    if not sess or sess["mode"] != "ideas_confirm":
        await edit(call, "Предпросмотр устарел — загрузи идеи заново.", KB([B("⬅️ В админ-панель", "m:admin")]))
        await ack(call)
        return
    clear_session(uid)
    if call.data == "upi:no":
        await edit(call, "Загрузка отменена.", KB([B("⬅️ В админ-панель", "m:admin")]))
        await ack(call)
        return
    items = sess["data"]["items"]
    with db() as con:
        for item in items:
            con.execute("INSERT INTO ideas(text,status,created_at,created_by) VALUES(?,?,?,?)",
                        (item, "new", now(), uid))
    await edit(call, f"✅ <b>Загружено идей: {len(items)}</b>", KB([B("⬅️ В админ-панель", "m:admin")]))
    await ack(call, "Готово")


async def handle_add_admin(message: Message):
    raw = (message.text or "").strip()
    raw = re.sub(r"^(?:https?://)?t\.me/", "", raw).lstrip("@").strip()
    cancel = KB([B("❌ Отмена", "adm:admins")])
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw):
        await message.answer("Пришли юзернейм в формате <code>@username</code>.", reply_markup=cancel)
        return
    with db() as con:
        u = con.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (raw,)).fetchone()
    if not u:
        await message.answer(
            f"Не нашёл @{esc(raw)} среди пользователей бота.\n\n"
            "Пусть человек откроет бота и нажмёт «Запустить», затем отправь юзернейм снова.",
            reply_markup=cancel,
        )
        return
    if is_admin(u["user_id"]):
        clear_session(message.from_user.id)
        await message.answer("Этот человек уже администратор.", reply_markup=KB([B("🛡 К списку админов", "adm:admins")]))
        return
    clear_session(message.from_user.id)
    await message.answer(
        f"Выдать <b>полный доступ</b> администратора?\n\n{user_link(u['user_id'], u['full_name'], u['username'])}",
        reply_markup=KB([B("✅ Выдать доступ", f"adm:addyes:{u['user_id']}")], [B("❌ Отмена", "adm:admins")]),
    )


async def handle_edit_idea(message: Message, iid):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправь новый текст идеи.", reply_markup=KB([B("❌ Отмена", f"ed:no:{iid}")]))
        return
    with db() as con:
        con.execute("UPDATE ideas SET text=? WHERE id=?", (text, iid))
    clear_session(message.from_user.id)
    await message.answer("✅ Текст обновлён")
    await show_idea(message, "all", iid, exact=False)


# ───────────────────────── обработка свободных сообщений ─────────────────────────

@dp.message()
async def on_message(message: Message):
    uid = message.from_user.id
    sess = get_session(uid)
    if not sess:
        text, kb = screen_hub(message.from_user)
        await message.answer("Я понимаю только кнопки 🙂 Вот меню:", reply_markup=None)
        await message.answer(text, reply_markup=kb)
        return
    mode, ref = sess["mode"], sess["ref"]
    admin_modes = {"admin_chat", "upload_ideas", "upload_mod", "upload_launcher", "edit_idea", "add_admin"}
    if mode in admin_modes and not is_admin(uid):
        clear_session(uid)
        return
    if mode == "new_idea":
        await handle_new_idea(message)
    elif mode == "chat":
        await handle_user_chat(message, ref)
    elif mode == "admin_chat":
        await handle_admin_chat(message, ref)
    elif mode == "upload_ideas":
        await handle_upload_ideas(message)
    elif mode in ("upload_mod", "upload_launcher"):
        await handle_upload_file(message, mode.split("_", 1)[1])
    elif mode == "edit_idea":
        await handle_edit_idea(message, ref)
    elif mode == "add_admin":
        await handle_add_admin(message)
    elif mode in ("file_confirm", "ideas_confirm"):
        await message.answer("Подтверди или отмени действие кнопками выше 👆")
    else:
        clear_session(uid)


# ───────────────────────── служебное ─────────────────────────

class Touch(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            try:
                with db() as con:
                    con.execute(
                        "INSERT INTO users(user_id,username,full_name,first_seen) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
                        (u.id, u.username, u.full_name, now()),
                    )
            except Exception:
                logging.exception("Не удалось обновить пользователя")
        return await handler(event, data)


dp.message.outer_middleware(Touch())
dp.callback_query.outer_middleware(Touch())


@dp.error()
async def on_error(event):
    logging.error("Необработанная ошибка", exc_info=event.exception)
    return True


async def main():
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
        BotCommand(command="id", description="Узнать свой Telegram ID"),
    ])
    try:
        await bot.set_my_description(description=BOT_DESCRIPTION)
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
    except TelegramAPIError:
        logging.exception("Не удалось задать описание бота")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
