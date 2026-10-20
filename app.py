import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Голосовой ассистент")
DB = Path(__file__).with_name("assistant.db")
pending = {}

DAYS = {
    "понедельник": 0, "вторник": 1, "среда": 2,
    "среду": 2, "четверг": 3, "пятница": 4,
    "пятницу": 4, "суббота": 5, "субботу": 5,
    "воскресенье": 6,
}
KINDS = {"task": "Задача", "reminder": "Напоминание",
         "meeting": "Встреча"}


# ---------------- DATABASE ----------------

def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


with connect() as db:
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            event_time TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        )
    """)


def normalize(text):
    text = text.lower().replace("ё", "е")
    return re.sub(r"[^\w\s.:/-]", " ", text).strip()


def save(kind, title, when=None):
    with connect() as db:
        cur = db.execute("""
            INSERT INTO events(kind,title,event_time,created_at)
            VALUES(?,?,?,?)
        """, (
            kind, title,
            when.isoformat(timespec="minutes") if when else None,
            datetime.now().isoformat(timespec="seconds")
        ))
        return cur.lastrowid


# ---------------- DATE PARSING ----------------

def parse_date(text):
    now = datetime.now().replace(second=0, microsecond=0)
    s = normalize(text)
    dt, has_date, has_time = now, False, False

    relative = re.search(r"через (\d+) (час\w*|минут\w*)", s)
    if relative:
        n = int(relative.group(1))
        dt = now + (timedelta(hours=n) if relative.group(2).startswith("час")
                    else timedelta(minutes=n))
        return dt, True, True

    if "послезавтра" in s:
        dt += timedelta(days=2)
        has_date = True
    elif "завтра" in s:
        dt += timedelta(days=1)
        has_date = True
    elif "сегодня" in s:
        has_date = True
    elif "следующей неделе" in s:
        dt += timedelta(days=7 - now.weekday())
        has_date = True
    else:
        for day, weekday in DAYS.items():
            if re.search(r"\bв " + day + r"\b", s):
                delta = (weekday - now.weekday()) % 7 or 7
                dt += timedelta(days=delta)
                has_date = True
                break

    explicit = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b", s)
    if explicit:
        d, m, y = explicit.groups()
        try:
            year = int(y) if y else now.year
            dt = dt.replace(year=year, month=int(m), day=int(d))
            if not y and dt < now:
                dt = dt.replace(year=year + 1)
            has_date = True
        except ValueError:
            pass

    clock = re.search(
        r"\bв\s*(\d{1,2})(?:[:.](\d{2}))?\s*(утра|вечера|дня|ночи)?",
        s
    )
    if clock:
        h = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        suffix = clock.group(3) or ""
        if suffix in ("вечера", "дня") and h < 12:
            h += 12
        if suffix == "ночи" and h == 12:
            h = 0
        if suffix == "утра" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= minute <= 59:
            dt = dt.replace(hour=h, minute=minute)
            has_time = True

    return dt, has_date, has_time


# ---------------- INTENT AND TITLE ----------------

def intent(text):
    s = normalize(text)

    if re.search(r"\b(найди|найти|отыщи|поищи|когда была|когда был)\b", s):
        return "search"

    if re.search(r"\b(что у меня|какие планы|покажи|есть ли|"
                 r"что запланировано|мои задачи|мои встречи)\b", s):
        return "list"

    if re.search(r"\b(встреча|встречу|совещание|созвон)\b", s):
        return "meeting"

    if re.search(r"\b(напомни|напомнить|напоминание|не забудь)\b", s):
        return "reminder"

    if re.search(r"\b(задача|задачу|поставь|добавь|создай|"
                 r"запланируй|нужно|надо|сделать)\b", s):
        return "task"

    return "unknown"


def title_from(text):
    s = normalize(text)
    prefixes = [
        r"^напомни мне ", r"^напомни ", r"^напомнить ",
        r"^поставь задачу ", r"^добавь ", r"^создай задачу ",
        r"^создай ", r"^запланируй ", r"^не забудь ",
        r"^мне нужно ", r"^нужно ", r"^надо ",
    ]
    for pattern in prefixes:
        s = re.sub(pattern, "", s, count=1)

    s = re.sub(
        r"\b(сегодня|завтра|послезавтра|"
        r"следующей неделе|до конца недели)\b", "", s
    )
    s = re.sub(r"\bчерез \d+ (?:час\w*|минут\w*)", "", s)
    s = re.sub(r"\bв \d{1,2}(?:[:.]\d{2})?\s*(?:утра|вечера|дня|ночи)?", "", s)
    s = re.sub(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{4})?\b", "", s)

    for day in DAYS:
        s = re.sub(r"\bв " + day + r"\b", "", s)

    s = re.sub(r"\s+", " ", s).strip(" .,!?:-")
    return s[:1].upper() + s[1:] if s else ""


# ---------------- LIST AND SEARCH ----------------

def show_events(text):
    now = datetime.now()
    s = normalize(text)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if "послезавтра" in s:
        start += timedelta(days=2)
        end = start + timedelta(days=1)
    elif "завтра" in s:
        start += timedelta(days=1)
        end = start + timedelta(days=1)
    elif "недел" in s:
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    else:
        end = start + timedelta(days=1)

    sql = """SELECT * FROM events
             WHERE status='active' AND event_time>=? AND event_time<?
          """
    params = [start.isoformat(timespec="minutes"),
              end.isoformat(timespec="minutes")]

    if "встреч" in s:
        sql += " AND kind='meeting'"
    elif "задач" in s:
        sql += " AND kind='task'"
    elif "напомин" in s:
        sql += " AND kind='reminder'"

    sql += " ORDER BY event_time"
    with connect() as db:
        return db.execute(sql, params).fetchall()


def search_events(text):
    ignored = {"найди", "найти", "задачу", "задача", "встречу",
               "встреча", "про", "когда", "была", "был", "мне"}
    words = {w for w in normalize(text).split()
             if w not in ignored and len(w) > 1}

    with connect() as db:
        rows = db.execute("SELECT * FROM events").fetchall()

    result = []
    for row in rows:
        title = normalize(row["title"])
        overlap = len(words & set(title.split()))
        ratio = SequenceMatcher(None, " ".join(words), title).ratio()
        score = max(ratio, overlap / max(len(words), 1))
        if score >= 0.25:
            result.append((score, row))

    result.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in result[:10]]


def format_event(row):
    when = row["event_time"]
    if when:
        when = datetime.fromisoformat(when).strftime("%d.%m.%Y в %H:%M")
    else:
        when = "дата не указана"
    return f'{KINDS[row["kind"]]}: {row["title"]} — {when}'


# ---------------- ASSISTANT ----------------

def answer(message, session="default"):
    s = normalize(message)
    if not s:
        return "Напишите запрос."

    # Ответ на уточняющий вопрос о встрече.
    if session in pending:
        dt, _, has_time = parse_date(s)
        if not has_time:
            clock = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\b", s)
            if clock:
                h = int(clock.group(1))
                m = int(clock.group(2) or 0)
                if 0 <= h <= 23 and 0 <= m <= 59:
                    dt = datetime.now().replace(hour=h, minute=m)
                    has_time = True
        if has_time:
            item = pending.pop(session)
            dt = item["date"].replace(hour=dt.hour, minute=dt.minute)
            save("meeting", item["title"], dt)
            return f'Встреча «{item["title"]}» добавлена: {dt:%d.%m.%Y в %H:%M}.'
        return "Укажите время, например 15:30."

    action = intent(s)

    if action in ("task", "reminder", "meeting"):
        dt, has_date, has_time = parse_date(s)
        title = title_from(s)

        if not title:
            return "Что именно нужно добавить? Укажите название."

        if action == "meeting" and not has_time:
            pending[session] = {
                "title": title,
                "date": dt if has_date else datetime.now()
            }
            if has_date:
                return f"На какое время поставить встречу «{title}»?"
            return f"На какую дату и время поставить встречу «{title}»?"

        save(action, title, dt if has_date or has_time else None)
        if has_date or has_time:
            return f'Готово! {KINDS[action]} «{title}» сохранена на {dt:%d.%m.%Y в %H:%M}.'
        return f'{KINDS[action]} «{title}» сохранена. Дата пока не указана.'

    if action == "list":
        rows = show_events(s)
        if not rows:
            return "На этот период ничего не запланировано."
        return "Ваши планы:\n" + "\n".join(
            f"{i}. {format_event(row)}" for i, row in enumerate(rows, 1)
        )

    if action == "search":
        query = re.sub(
            r"\b(найди|найти|отыщи|поищи|задачу|задача|"
            r"встречу|встреча|про|когда|была|был)\b", "", s
        ).strip()
        rows = search_events(query)
        if not rows:
            return "Не нашёл подходящих записей."
        return "Нашёл:\n" + "\n".join(format_event(row) for row in rows)

    return (
        "Я умею создавать задачи, напоминания и встречи, "
        "показывать расписание и искать записи.\n\n"
        "Пример: «Напомни завтра в 10 позвонить Ивану»."
    )


# ---------------- API ----------------

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@app.post("/chat")
def chat(request: ChatRequest):
    return {"reply": answer(request.message, request.session_id)}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------- WEB PAGE ----------------

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Голосовой помощник</title>
<style>
body{font:16px Arial;background:#f3f5f9;margin:30px auto;
max-width:750px;padding:0 15px;color:#222}
#chat{height:380px;overflow:auto;background:white;padding:16px;
border:1px solid #ddd;border-radius:12px;white-space:pre-wrap}
.msg{padding:10px;margin:8px 0;border-radius:8px;overflow-wrap:anywhere}
.user{background:#e8f0ff;color:#174ea6}
.bot{background:#eaf7ed;color:#176534}
form{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
input{flex:1;min-width:180px;padding:12px;border:1px solid #bbb;
border-radius:8px;font-size:16px}
button{padding:12px;border:0;border-radius:8px;background:#2563eb;
color:white;cursor:pointer}
#status{color:#666;font-size:13px}
</style>
</head>
<body>
<h1>Голосовой помощник</h1>
<p>Задачи, напоминания и встречи без ИИ.</p>
<div id="chat" aria-live="polite">
<div class="msg bot">Ассистент: Привет! Чем могу помочь?</div>
</div>
<form id="form">
<input id="input" placeholder="Что у меня завтра?" required>
<button>Отправить</button>
<button type="button" id="mic">🎙 Голос</button>
</form>
<p id="status">Введите запрос или нажмите микрофон.</p>
<script>
const chat=document.querySelector("#chat");
const input=document.querySelector("#input");
const status=document.querySelector("#status");
const session=localStorage.getItem("assistant_id")||
("s"+Math.random().toString(36).slice(2));
localStorage.setItem("assistant_id",session);

function add(cls,text){
 const d=document.createElement("div");
 d.className="msg "+cls;
 d.textContent=(cls==="user"?"Вы: ":"Ассистент: ")+text;
 chat.appendChild(d);chat.scrollTop=chat.scrollHeight;
}

async function send(text){
 text=text.trim();if(!text)return;
 add("user",text);input.value="";status.textContent="Обрабатываю...";
 try{
  const r=await fetch("/chat",{method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({message:text,session_id:session})});
  if(!r.ok)throw Error();
  const data=await r.json();add("bot",data.reply);
  status.textContent="Готово.";
  if("speechSynthesis" in window){
   speechSynthesis.cancel();
   const u=new SpeechSynthesisUtterance(data.reply);
   u.lang="ru-RU";speechSynthesis.speak(u);
  }
 }catch(e){add("bot","Ошибка соединения с сервером.");status.textContent="Ошибка";}
}

document.querySelector("#form").addEventListener("submit",e=>{
 e.preventDefault();send(input.value);
});

const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
document.querySelector("#mic").addEventListener("click",()=>{
 if(!Recognition){
  status.textContent="Голосовой ввод недоступен. Попробуйте Chrome.";
  return;
 }
 const rec=new Recognition();rec.lang="ru-RU";
 rec.onstart=()=>status.textContent="Слушаю...";
 rec.onresult=e=>send(e.results[0][0].transcript);
 rec.onerror=e=>status.textContent="Ошибка микрофона: "+e.error;
 rec.start();
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)
