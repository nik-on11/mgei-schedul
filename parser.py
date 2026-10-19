"""
parser.py — парсер расписания МГЭИ им. А.Д. Сахарова БГУ (rsp.iseu.by).

Сайт построен на ASP.NET WebForms, поэтому «просто сделать GET» недостаточно:
каждый выбор в выпадающем списке — это POST-запрос («postback»), в котором нужно
вернуть серверу скрытые поля __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION
и т.д., полученные в предыдущем ответе.

Порядок обхода:
    факультет -> форма обучения -> курс -> группа -> недели -> таблица расписания

Результат сохраняется в schedules.json:
    {"ИТ-11": {"0": [{"time": ..., "subject": ..., "room": ..., "teacher": ...}], "1": [], ...}}
    где 0 = понедельник ... 6 = воскресенье.

Запуск отдельно:  python parser.py
Из бота:          from parser import update_schedules
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ============================================================
# НАСТРОЙКИ — если парсер «не видит» списки или таблицу, правь здесь
# ============================================================

BASE_URL = "http://rsp.iseu.by/Raspisanie/TimeTable/umu.aspx"
OUTPUT_FILE = Path(__file__).with_name("schedules.json")          # куда сохраняем результат
DEBUG_HTML_FILE = Path(__file__).with_name("debug_last_page.html")  # сюда пишем HTML при ошибке

# Что именно парсим (поиск по вхождению подстроки, регистр не важен)
FACULTY_KEYWORDS: list[str] = []   # пусто = все факультеты; пример: ["мониторинга"]
FORM_KEYWORDS: list[str] = []      # пусто = все формы обучения; пример: ["дневная"]
WEEKS_BACK = 1                     # сколько прошлых недель сохранять
WEEKS_AHEAD = 3                    # сколько будущих недель сохранять

# Точные атрибуты name выпадающих списков. Если None — ищем автоматически по подсказкам ниже.
# Пример: "faculty": "ctl00$ContentPlaceHolder1$ddlFaculty"
# Реальные имена смотри в браузере: F12 -> Elements -> найди <select name="...">.
# Имена списков на rsp.iseu.by (взяты из HTML страницы)
SELECT_NAMES: dict[str, str | None] = {
    "faculty": "ddlFac",
    "department": "ddlDep",   # форма обучения (дневная / заочная)
    "course": "ddlCourse",
    "group": "ddlGroup",
    "week": "ddlWeek",
}

# Подсказки для автоматического поиска: ищем эти фрагменты в name/id тега <select>
SELECT_HINTS = {
    "faculty": ("facult", "fak", "фак"),
    "department": ("dep", "форм"),
    "course": ("kurs", "cours", "curs", "курс"),
    "group": ("group", "grup", "grp", "групп"),
}

# id таблицы с расписанием (если известен). None — ищем автоматически.
TABLE_ID: str | None = None

REQUEST_DELAY = 0.3   # пауза между запросами (сек), чтобы не нагружать сайт
TIMEOUT = 30          # таймаут одного запроса (сек)
RETRIES = 3           # сколько раз повторять запрос при сетевой ошибке

# Порядок уровней обхода
LEVELS = ["faculty", "department", "course", "group"]

# ============================================================
# Регулярные выражения и справочники
# ============================================================

# Время: «09:00-10:30», «9.00 – 10.30» и т.п.
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
# Преподаватель: «Иванов И.И.» / «Иванов И. И.»
TEACHER_RE = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.?")
# Аудитория: «ауд. 101», «каб. 12а», «спортзал» или просто трёхзначное число
ROOM_RE = re.compile(r"(?:ауд\.?|каб\.?)\s*[\w\-/]+|спортзал|\b\d{3}[а-яa-z]?\b", re.IGNORECASE)

DAY_FULL = {
    "понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3,
    "пятница": 4, "суббота": 5, "воскресенье": 6,
}
DAY_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


# ============================================================
# Работа с сетью и ASP.NET-формами
# ============================================================

def _request(session: requests.Session, method: str, **kwargs) -> BeautifulSoup:
    """Делает запрос с повторами при ошибках и возвращает HTML, разобранный в BeautifulSoup."""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.request(method, BASE_URL, timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            # Иногда сервер не указывает кодировку — тогда угадываем её по содержимому
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            time.sleep(REQUEST_DELAY)
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            log.warning("Ошибка запроса (попытка %d/%d): %s", attempt, RETRIES, exc)
            if attempt == RETRIES:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")  # чтобы линтеры не ругались


def collect_form_fields(soup: BeautifulSoup) -> dict[str, str]:
    """
    Собирает ВСЕ поля формы так, как это сделал бы браузер:
    скрытые поля (__VIEWSTATE, __EVENTVALIDATION, __VIEWSTATEGENERATOR ...),
    текстовые поля и текущие значения всех <select>.
    """
    form = soup.find("form") or soup
    data: dict[str, str] = {}

    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        input_type = (inp.get("type") or "text").lower()
        if input_type in ("submit", "button", "image", "reset", "file"):
            continue  # кнопки браузер отправляет только если на них нажали
        if input_type in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
            continue
        data[name] = inp.get("value", "")

    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        option = select.find("option", selected=True) or select.find("option")
        if option is not None:
            data[name] = option.get("value", option.get_text(strip=True))

    return data


def post_back(
    session: requests.Session,
    soup: BeautifulSoup,
    select_name: str | None = None,
    value: str | None = None,
    button: tuple[str, str] | None = None,
) -> BeautifulSoup:
    """
    Эмулирует действие пользователя на странице:
      • выбор значения в <select> (select_name + value) — срабатывает AutoPostBack;
      • или нажатие кнопки (button = (name, value)).
    Состояние (__VIEWSTATE и др.) берём из переданной страницы `soup`.
    """
    payload = collect_form_fields(soup)
    payload["__EVENTARGUMENT"] = ""
    payload["__EVENTTARGET"] = select_name if (select_name and not button) else ""
    if select_name and value is not None:
        payload[select_name] = value
    if button:
        payload[button[0]] = button[1]

    headers = {"Referer": BASE_URL}
    return _request(session, "POST", data=payload, headers=headers)


# ============================================================
# Поиск списков и их значений
# ============================================================

def find_select(soup: BeautifulSoup, role: str):
    """Находит <select> нужной «роли» (faculty/speciality/course/group) или None."""
    forced = SELECT_NAMES.get(role)
    if forced:
        return soup.find("select", attrs={"name": forced})

    for select in soup.find_all("select"):
        ident = f"{select.get('name', '')} {select.get('id', '')}".casefold()
        if any(hint in ident for hint in SELECT_HINTS[role]):
            return select
    return None


def list_options(select, role: str) -> list[tuple[str, str]]:
    """Возвращает [(value, text), ...] из <select>, отбрасывая заглушки и лишнее по фильтрам."""
    keywords = {"faculty": FACULTY_KEYWORDS, "department": FORM_KEYWORDS}.get(role)
    result = []
    for option in select.find_all("option"):
        text = option.get_text(strip=True)
        value = option.get("value", text)
        # Пропускаем «— выберите —» и пустые значения
        if not text or value in ("", "-1") or "выбер" in text.casefold():
            continue
        if keywords and not any(k.casefold() in text.casefold() for k in keywords):
            continue
        result.append((value, text))
    return result


# ============================================================
# Разбор таблицы расписания
# ============================================================

def match_day(text: str) -> int | None:
    """Если текст — название дня недели, возвращает его номер (0–6), иначе None."""
    t = text.strip().casefold()
    if not t:
        return None
    if t in DAY_SHORT:
        return DAY_SHORT[t]
    for name, idx in DAY_FULL.items():
        if t.startswith(name):
            return idx
    return None


def normalize_time(text: str) -> str | None:
    """'9.00-10.30' -> '09:00–10:30'. Если времени в тексте нет — None."""
    m = TIME_RE.search(text)
    if not m:
        return None
    h1, m1, h2, m2 = m.groups()
    return f"{int(h1):02d}:{m1}–{int(h2):02d}:{m2}"


def _normalize_room(room: str) -> str:
    """Приводит аудиторию к виду 'ауд. 101'."""
    room = room.strip()
    if room[0].isdigit():
        return f"ауд. {room}"
    return re.sub(r"^(ауд|каб)\.?\s*", lambda m: m.group(1).lower() + ". ", room, flags=re.IGNORECASE)


# --- Разбор строки занятия вида 'лек. Физика доц. Луцевич Александр Александрович ауд. 103 , К2' ---
# Должность ищем без учёта регистра, а вот имена — СТРОГО с заглавной буквы. Так слова из названия
# предмета («Математический анализ») больше не принимаются за ФИО преподавателя.
_RANK = r"(?i:доц|проф|ст\.?\s*преп|ст\.?\s*пр|зав\.?\s*каф|преп|асс|пр)"
_NAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_INITIALS = rf"{_NAME}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.?"          # Иванов И.И.
_FULL = rf"{_NAME}(?:\s+{_NAME}){{1,2}}"                   # Иванов Иван Иванович
STAFF_RE = re.compile(rf"(?<![А-Яа-яЁё]){_RANK}\.?\s+(?:{_INITIALS}|{_FULL})|{_INITIALS}")
CORPUS_RE = re.compile(r"\b[КK][\dЗ]\b")                    # корпус: К1, К2, К3 ...
SUBGROUP_RE = re.compile(r"(\d)\s*п/гр\.?", re.IGNORECASE)   # подгруппа: «1 п/гр»


def build_lesson(time_str: str, raw: str) -> dict[str, str]:
    """Разбирает сырой текст занятия на предмет, аудиторию (с корпусом) и преподавателей."""
    text = " ".join(p.strip() for p in re.split(r"[\n\r|]+", raw) if p.strip())
    original = text

    # 1. Подгруппа («1 п/гр») — выносим и добавим в конец названия
    subgroup = ""
    m = SUBGROUP_RE.search(text)
    if m:
        subgroup = f"{m.group(1)} п/гр"
        text = text[:m.start()] + " " + text[m.end():]

    # 2. Корпус (К1, К2 ...)
    corpus = ""
    m = CORPUS_RE.search(text)
    if m:
        corpus = m.group(0).upper()
        text = text[:m.start()] + " " + text[m.end():]

    # 3. Преподаватели. Идём с конца, чтобы вырезание не сбивало позиции остальных совпадений
    teachers = []
    for m in reversed(list(STAFF_RE.finditer(text))):
        if m.start() == 0:   # в самом начале стоит тип занятия («пр. ...»), а не преподаватель
            continue
        teachers.append(m.group(0).strip(" ,;"))
        text = text[:m.start()] + " " + text[m.end():]
    teachers.reverse()

    # 4. Аудитория
    room = ""
    m = ROOM_RE.search(text)
    if m:
        room = _normalize_room(m.group(0))
        text = text[:m.start()] + " " + text[m.end():]
    if corpus:
        room = f"{room} ({corpus})" if room else corpus

    # 5. Всё, что осталось, — название предмета (вместе с типом: «лек.», «лаб.», «практ. зан.»)
    subject = re.sub(r"\s+,", ",", re.sub(r"\s+", " ", text)).strip(" ,;.-–—")
    if not subject:
        subject = original   # на крайний случай: лучше показать сырой текст, чем прочерк
    if subgroup:
        subject = f"{subject} ({subgroup})"

    return {"time": time_str, "subject": subject, "room": room, "teacher": ", ".join(dict.fromkeys(teachers))}


def find_schedule_table(soup: BeautifulSoup):
    """Ищет таблицу с расписанием: по TABLE_ID или как «самую насыщенную временем» без вложенных таблиц."""
    if TABLE_ID:
        return soup.find("table", id=TABLE_ID)

    best, best_score = None, 0
    for table in soup.find_all("table"):
        if table.find("table"):
            continue  # это «обёртка» (вёрстка), нам нужна самая внутренняя таблица
        score = len(TIME_RE.findall(table.get_text(" ")))
        if score > best_score:
            best, best_score = table, score
    return best


def parse_table(table) -> dict[str, list[dict[str, str]]]:
    """
    Разбирает таблицу в {"0": [...], ..., "6": [...]}.
    Поддерживаются две популярные раскладки:
      A) дни — в строках (ячейка с днём, дальше строки «время | предмет...»);
      B) дни — в столбцах (шапка с днями, слева столбец времени).
    """
    schedule: dict[str, list[dict[str, str]]] = {str(i): [] for i in range(7)}
    rows = table.find_all("tr")

    # --- Раскладка B: ищем строку-шапку, где подряд идут >= 3 названий дней
    day_columns: dict[int, int] = {}
    header_index = -1
    for r_idx, row in enumerate(rows):
        cells = row.find_all(["td", "th"])
        found = {}
        for c_idx, cell in enumerate(cells):
            day = match_day(cell.get_text(" ", strip=True))
            if day is not None:
                found[c_idx] = day
        if len(found) >= 3:
            day_columns, header_index = found, r_idx
            break

    if day_columns:
        for row in rows[header_index + 1:]:
            cells = row.find_all(["td", "th"])
            texts = [c.get_text("\n", strip=True) for c in cells]
            time_str = next((normalize_time(t) for t in texts if normalize_time(t)), None)
            if not time_str:
                continue
            for col, day in day_columns.items():
                if col < len(texts) and texts[col] and not TIME_RE.search(texts[col]):
                    schedule[str(day)].append(build_lesson(time_str, texts[col]))
        return schedule

    # --- Раскладка A: идём по строкам, запоминая текущий день
    current_day: int | None = None
    for row in rows:
        cells = row.find_all(["td", "th"])
        texts = [c.get_text("\n", strip=True) for c in cells]

        day_cell_idx = None
        for i, t in enumerate(texts):
            day = match_day(t)
            if day is not None:
                current_day, day_cell_idx = day, i
                break

        time_cell_idx = next((i for i, t in enumerate(texts) if TIME_RE.search(t)), None)
        if current_day is None or time_cell_idx is None:
            continue

        time_str = normalize_time(texts[time_cell_idx])
        others = [
            t for i, t in enumerate(texts)
            if i not in (day_cell_idx, time_cell_idx) and t and not re.fullmatch(r"\d{1,2}", t)
        ]
        if not others:
            continue  # пустая пара
        schedule[str(current_day)].append(build_lesson(time_str, "\n".join(others)))

    return schedule


# ============================================================
# Обход иерархии и сохранение
# ============================================================

def _dump_debug(soup: BeautifulSoup) -> None:
    """Сохраняет текущий HTML в файл — удобно смотреть, что именно вернул сервер."""
    DEBUG_HTML_FILE.write_text(str(soup), encoding="utf-8")
    log.error("HTML последней страницы сохранён в %s", DEBUG_HTML_FILE)


_ctx: dict = {"weeks": [], "current": "", "last": None}   # недели для загрузки и последняя страница (для отладки)


def _setup_weeks(select) -> None:
    """Выбираем недели вокруг текущей (в ddlWeek значения вида '19.10.2026 0:00:00')."""
    opts = []
    for option in (select.find_all("option") if select is not None else []):
        value = option.get("value", "")
        try:
            key = datetime.strptime(value.split()[0], "%d.%m.%Y").strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue
        is_current = "current-week-option" in (option.get("class") or []) or option.has_attr("selected")
        opts.append((key, value, is_current))
    if not opts:
        raise RuntimeError("Список недель (ddlWeek) не найден — проверь SELECT_NAMES['week'] в parser.py")
    opts.sort()  # от старых недель к новым
    cur = next((i for i, o in enumerate(opts) if o[2]), len(opts) - 1)
    chosen = opts[max(0, cur - WEEKS_BACK): cur + WEEKS_AHEAD + 1]
    _ctx["weeks"] = [(k, v) for k, v, _ in chosen]
    _ctx["current"] = opts[cur][0]
    log.info("Недели для загрузки: %s", ", ".join(k for k, _ in _ctx["weeks"]))


def _handle_group(session, soup: BeautifulSoup, group_name: str, result: dict, path: dict | None = None) -> None:
    """
    Для выбранной группы запрашиваем каждую неделю из _ctx["weeks"]:
    POST с нужным значением ddlWeek и нажатием кнопки «Показать».
    """
    button = soup.find("input", attrs={"type": "submit"})
    btn = (button["name"], button.get("value", "")) if button is not None and button.get("name") else None

    weeks: dict[str, dict] = {}
    total = 0
    for key, value in _ctx["weeks"]:
        page = post_back(session, soup, select_name=SELECT_NAMES["week"], value=value, button=btn)
        _ctx["last"] = page
        table = find_schedule_table(page)
        week = parse_table(table) if table is not None else {str(i): [] for i in range(7)}
        weeks[key] = week
        total += sum(len(week[str(i)]) for i in range(7))

    if group_name in result:
        log.warning("Группа %s встречается повторно — данные будут перезаписаны", group_name)
    entry = {"_meta": dict(path or {}), "weeks": weeks}
    entry.update(weeks[_ctx["current"]])   # дни текущей недели дублируем наверх — их читает бот
    result[group_name] = entry
    log.info("Группа %s: %d пар за %d нед.", group_name, total, len(weeks))


def _walk(session, soup: BeautifulSoup, level: int, result: dict, path: dict | None = None) -> None:
    """Рекурсивный обход: на каждом уровне перебираем варианты списка и делаем postback."""
    if level >= len(LEVELS):
        return

    path = path or {}
    role = LEVELS[level]
    select = find_select(soup, role)

    if select is None:
        names = [s.get("name") for s in soup.find_all("select")]
        log.warning("Список «%s» не найден. Доступные <select>: %s", role, names)
        if role != "group":
            _walk(session, soup, level + 1, result, path)  # возможно, этого уровня на сайте нет
        else:
            _dump_debug(soup)
        return

    for value, text in list_options(select, role):
        log.info("[%s] %s", role, text)
        # Каждый раз берём состояние формы из ТЕКУЩЕЙ страницы (soup) — так соседние варианты независимы
        new_soup = post_back(session, soup, select_name=select["name"], value=value)
        if role == "group":
            _handle_group(session, new_soup, text, result, path)
        else:
            _walk(session, new_soup, level + 1, result, {**path, role: text})


def update_schedules() -> dict:
    """
    Главная функция: обходит сайт и перезаписывает schedules.json.
    Блокирующая (использует requests) — из асинхронного кода вызывать через asyncio.to_thread.
    Возвращает словарь с расписаниями. Если не найдено ни одной группы — бросает RuntimeError,
    а старый schedules.json остаётся нетронутым.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; ScheduleBot/1.0)",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })

    start_page = _request(session, "GET")
    _setup_weeks(start_page.find("select", attrs={"name": SELECT_NAMES["week"]}))
    result: dict = {}
    _walk(session, start_page, 0, result)

    if not result:
        raise RuntimeError(
            "Не удалось получить ни одной группы. Проверь настройки в начале parser.py "
            f"(SELECT_NAMES, TABLE_ID) и файл {DEBUG_HTML_FILE.name}."
        )

    total = sum(len(lessons) for g in result.values() for wk in g["weeks"].values() for lessons in wk.values())
    if total == 0:   # таблицы не разобраны ни у одной группы — старый файл не трогаем
        if _ctx["last"] is not None:
            _dump_debug(_ctx["last"])
        raise RuntimeError(f"Не найдено ни одной пары. Пришли файл {DEBUG_HTML_FILE.name} — подправлю разбор таблицы.")

    # Атомарная запись: сначала во временный файл, потом подмена — файл не повредится при сбое
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUTPUT_FILE)
    log.info("Сохранено групп: %d -> %s", len(result), OUTPUT_FILE)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    update_schedules()
