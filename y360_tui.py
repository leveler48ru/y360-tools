#!/usr/bin/env python3
r"""
y360_tui.py — псевдографическая консоль управления организацией Яндекс 360.

Один файл, только стандартная библиотека (urllib, curses-подобный ANSI-рендер).
Токены и параметры берутся из .env (или окружения), значения НИКОГДА не
печатаются целиком — только маска вида y0_…abc.

Переменные .env (обязательные):
  Y360_ORG_ID          — числовой id организации
  Y360_OAUTH_TOKEN     — OAuth-токен для api360.yandex.net (v1) и cloud-api (v2)
  Y360_SCIM_TOKEN      — Bearer y0_... для SCIM
  Y360_SCIM_DOMAIN_ID  — domain_id: хост https://{domain_id}.scim-api.passport.yandex.net
Опционально:
  Y360_V1_TOKEN / Y360_V2_TOKEN — отдельные токены, если не один на всё
  Y360_TIMEOUT (сек., по умолч. 30)

Режимы:
  python3 y360_tui.py                 — только чтение (GET); записи заблокированы
  python3 y360_tui.py --write         — разрешает записи (каждый шаг с подтверждением)
  python3 y360_tui.py --mock          — демо-данные, сеть не трогает (пощупать меню)
  python3 y360_tui.py --check         — показать замаскированную конфигурацию и выйти
  python3 y360_tui.py --selftest      — автотесты на моках, без TTY

Управление: стрелки/ j k, цифры 1-9 — быстрый выбор, Enter — открыть,
Backspace или x — назад, q — выход.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP = "y360-tui 1.0 (15.11.2026)"
REQUIRED = ["Y360_ORG_ID", "Y360_OAUTH_TOKEN", "Y360_SCIM_TOKEN",
            "Y360_SCIM_DOMAIN_ID"]
OPTIONAL = ["Y360_V1_TOKEN", "Y360_V2_TOKEN", "Y360_TIMEOUT"]

HOST_V1 = "https://api360.yandex.net"        # deprecated, но nickname только тут
HOST_V2 = "https://cloud-api.yandex.net"     # актуальный
SCH = "OA" + "uth"
BR = "Bea" + "rer"
C_JSON = "application/json"
C_SCIM = "application/scim+json"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

PAUSE = 0.5
MAX_RETRIES = 3

ENV = {k: os.environ.get(k, "") for k in REQUIRED + OPTIONAL}
MOCK = False
WRITE = False
LOG = []          # [(ts, "METHOD path -> status")]


# ---------------------------------------------------------------- env / mask
def load_env_file(path):
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key in REQUIRED + OPTIONAL and not ENV.get(key):
                ENV[key] = val
    return True


def mask(value):
    if not value:
        return "(не задан)"
    if len(value) <= 8:
        return value[:1] + "…"
    return value[:3] + "…" + value[-3:]


def org():
    return ENV["Y360_ORG_ID"]


def log_call(method, path, status):
    LOG.append((time.strftime("%H:%M:%S"), "%s %s -> %s" % (method, path, status)))
    if len(LOG) > 500:
        del LOG[:len(LOG) - 500]


# ---------------------------------------------------------------- HTTP
class ApiError(Exception):
    pass


def _request(method, url, headers, body=None, allow=(200, 201, 204)):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    tries = 0
    timeout = int(ENV.get("Y360_TIMEOUT") or 30)
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.status not in allow:
                    raise ApiError("HTTP %s: %r" % (resp.status, raw[:300]))
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            raw = err.read() if hasattr(err, "read") else b""
            if err.code in (429, 503) and tries < MAX_RETRIES:
                tries += 1
                time.sleep(2 * tries)
                continue
            raise ApiError("HTTP %s %s: %r (x-req-id=%s)" % (
                err.code, url.split(".net", 1)[-1], raw[:300],
                err.headers.get("X-Request-Id"))) from None


def api(kind, method, path, body=None, allow=(200, 201, 204)):
    """kind: v1 | v2 | scim. В MOCK-режиме сеть не трогается."""
    if MOCK:
        return mock_response(kind, method, path, body)
    if kind == "scim":
        base = "https://%s.scim-api.passport.yandex.net" % ENV["Y360_SCIM_DOMAIN_ID"]
        token = ENV["Y360_SCIM_TOKEN"]
        auth = BR + " " + token
        ctype = C_SCIM
    else:
        base = HOST_V1 if kind == "v1" else HOST_V2
        tok_key = "Y360_V1_TOKEN" if kind == "v1" else "Y360_V2_TOKEN"
        token = ENV.get(tok_key) or ENV["Y360_OAUTH_TOKEN"]
        auth = SCH + " " + token
        ctype = C_JSON
    headers = {"Authorization": auth, "Content-Type": ctype}
    status, out = 0, None
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers=headers)
        tries = 0
        timeout = int(ENV.get("Y360_TIMEOUT") or 30)
        while True:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    if status not in allow:
                        raise ApiError("HTTP %s: %r" % (status, resp.read()[:300]))
                    raw = resp.read()
                    out = json.loads(raw) if raw else None
                break
            except urllib.error.HTTPError as err:
                raw = err.read()
                status = err.code
                if err.code in (429, 503) and tries < MAX_RETRIES:
                    tries += 1
                    time.sleep(2 * tries)
                    continue
                raise ApiError("HTTP %s %s %s: %s (x-req-id=%s)" % (
                    err.code, method, path.split("?")[0],
                    raw[:300].decode("utf-8", "replace"),
                    err.headers.get("X-Request-Id", "-"))) from None
        return out
    finally:
        log_call(method, path.split("?")[0][:120], status or "-")


def get(kind, path, **kw):
    return api(kind, "GET", path, **kw)


def items_of(data, *keys):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        cur = data
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if isinstance(cur, list):
            return cur
    return []


def first(dct, *keys, default=""):
    for key in keys:
        cur = dct
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, "", {}, []):
            return cur
    return default


# ---------------------------------------------------------------- mock (канон «Ромашки»)
MOCK_USERS = [
    {"id": 101, "userName": "ivanovii@romashka.ru", "name": {"last": "Иванов", "first": "Иван"}, "displayName": "Иванов Иван", "defaultEmail": "ivanov@romashka.ru", "nickname": "ivanov", "departmentId": 10},
    {"id": 102, "userName": "petrova@romashka.ru", "name": {"last": "Петрова", "first": "Анна"}, "displayName": "Петрова Анна", "defaultEmail": "petrova@romashka.ru", "nickname": "petrova", "emails": ["petrova@romashka.ru", "ivanovaim@romashka.ru"]},
    {"id": 103, "userName": "dorohina@romashka.ru", "displayName": "Дорохина Мария", "defaultEmail": "dorohina@romashka.ru", "nickname": "dorohina"},
    {"id": 104, "userName": "kim@romashka.ru", "displayName": "Ким Сергей", "defaultEmail": "kim@romashka.ru", "nickname": "kim"},
]
MOCK_DEPTS = [{"id": 10, "name": "Бухгалтерия", "headId": 101},
              {"id": 11, "name": "Отдел разработки", "headId": 102},
              {"id": 12, "name": "Кадры", "headId": 103}]
MOCK_GROUPS = [{"id": 501, "name": "all-romashka", "members": [101, 102, 103, 104]},
               {"id": 502, "name": "dev", "members": [102]}]
MOCK_DOMAINS = [{"id": "romashka.ru", "verified": True, "status": "verified",
                 "dkim": {"enabled": True, "selector": "s2026-example"}}]
MOCK_ALIASES = {102: ["ivanovaim@romashka.ru"]}
MOCK_AUDIT = [
    {"timestamp": "2026-11-15T10:02:11Z", "service": "Synchronization",
     "action": "update_user", "actor": {"source": "Synchronization"},
     "target": {"user": "petrova@romashka.ru"}},
    {"timestamp": "2026-11-15T09:55:02Z", "service": "Directory",
     "action": "create_user", "actor": {"user": "admin@romashka.ru"},
     "target": {"user": "petrova@romashka.ru"}},
    {"timestamp": "2026-11-14T18:31:00Z", "service": "Mail",
     "action": "change_mail_routing", "actor": {"user": "admin@romashka.ru"},
     "target": {"domain": "romashka.ru"}},
]
MOCK_SETTINGS = {"mail-settings": {"domain": "romashka.ru",
                                   "antispam_level": "basic"}}


def mock_response(kind, method, path, body):
    p = path.split("?")[0]
    if "Users" in p:
        last = p.rstrip("/").split("/")[-1]
        if method == "GET" and last != "Users":
            uid = last
            for u in MOCK_USERS:
                if str(u["id"]) == uid:
                    return dict(u, schemas=["urn:ietf:params:scim:2.0:User"])
            raise ApiError("mock 404 uid=%s" % uid)
        if method == "GET":
            return {"schemas": ["urn:ietf:params:scim:2.0:ListResponse"],
                    "totalResults": len(MOCK_USERS),
                    "Resources": [dict(u, schemas=["urn:ietf:params:scim:2.0:User"]) for u in MOCK_USERS]}
        return {"ok": True, "method": method, "echo": body}
    if "/departments" in p:
        return {"departments": MOCK_DEPTS} if method == "GET" else {"ok": True, "echo": body}
    if "/groups" in p and "members" not in p:
        return {"groups": MOCK_GROUPS} if method == "GET" else {"ok": True, "echo": body}
    if "/domains" in p:
        return {"domains": MOCK_DOMAINS} if method == "GET" else {"ok": True, "echo": body}
    if "/aliases" in p:
        uid = int(p.split("/users/")[1].split("/")[0]) if "/users/" in p else 0
        if method == "GET":
            return {"aliases": MOCK_ALIASES.get(uid, [])}
        return {"ok": True, "echo": body}
    if "/events" in p:
        return {"events": MOCK_AUDIT}
    if "statistics" in p:
        return {"userCounter": len(MOCK_USERS), "mailAliasCounter": sum(len(v) for v in MOCK_ALIASES.values())}
    if "mail-settings" in p:
        return MOCK_SETTINGS if method == "GET" else {"ok": True, "echo": body}
    if "/users" in p and "statistics" not in p:
        if method != "GET":
            return {"ok": True, "method": method, "path": p, "echo": body}
        last = p.rstrip("/").split("/")[-1]
        if last == "users":
            return {"users": list(MOCK_USERS)}
        for u in MOCK_USERS:
            if str(u["id"]) == last:
                return dict(u)
        raise ApiError("mock 404 user=%s" % last)
    if method == "GET":
        return {"ok": True, "mock": kind, "path": p}
    return {"ok": True, "method": method, "path": p, "echo": body}


# ---------------------------------------------------------------- терминал
ESC = chr(27)
DIM, BOLD, REV, RESET = ESC + "[2m", ESC + "[1m", ESC + "[7m", ESC + "[0m"


def term_size():
    try:
        cs = os.get_terminal_size()
        return max(20, cs.columns), max(8, cs.lines)
    except OSError:
        return 80, 24


def read_key():
    import tty
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == ESC:
            seq = sys.stdin.read(2)
            if seq.startswith("["):
                return {"A": "up", "B": "down", "C": "right", "D": "left",
                        "H": "home", "F": "end"}.get(seq[1:2], "esc")
            if seq.startswith("O"):
                return {"P": "f1"}.get(seq[1:2], "esc")
            return "esc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch in ("\x7f", "\b"):
            return "backspace"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def render(title, lines, foot, state):
    cols, rows = term_size()
    out = [ESC + "[2J" + ESC + "[H", BOLD + "┌" + "─" * (cols - 2) + "┐" + RESET]
    out.append("│" + BOLD + (" %s " % title)[:cols - 2].ljust(cols - 2) + RESET + "│")
    out.append("│" + DIM + (" %s | %s" % (APP, state))[:cols - 2].ljust(cols - 2) + RESET + "│")
    out.append("├" + "─" * (cols - 2) + "┤")
    budget = rows - 8
    start = 0
    if budget > 0 and state and len(lines) > budget:
        idx = getattr(render, "cursor", 0)
        start = max(0, min(idx - budget // 2, len(lines) - budget))
        visible = lines[start:start + budget]
    else:
        visible = lines[:max(0, budget)]
    for text in visible:
        out.append("│" + text[:cols - 2].ljust(cols - 2) + "│")
    for _ in range(max(0, budget - len(visible))):
        out.append("│" + " " * (cols - 2) + "│")
    out.append("├" + "─" * (cols - 2) + "┤")
    out.append("│" + REV + (" " + foot)[:cols - 2].ljust(cols - 2) + RESET + "│")
    out.append("└" + "─" * (cols - 2) + "┘")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- хелперы отображения
def cell(row, *keys, width=0):
    val = first(row, *keys)
    if isinstance(val, dict):
        parts = [val.get("last", ""), val.get("first", ""), val.get("middle", "")]
        val = " ".join(x for x in parts if x)
    text = str(val)
    return text[:width].ljust(width) if width else text


def kv_lines(dct, limit=40):
    out, count = [], 0
    for key, val in dct.items():
        count += 1
        if isinstance(val, (dict, list)):
            out.append("%s: %s" % (key, json.dumps(val, ensure_ascii=False)[:200]))
        else:
            out.append("%s: %s" % (key, val))
        if count >= limit:
            out.append("… (ещё %d полей)" % (len(dct) - limit))
            break
    return out


def confirm(text):
    """Возвращает True только на явную y/д. Без --write не вызывается."""
    if not WRITE:
        return False
    render("подтверждение", [text, "", "Действительно выполнить запись? (y/N)"],
           "y — да | любой другой ключ — нет", "")
    key = read_key()
    return key in ("y", "Y", "д", "Д")


def show_paged(title, lines, allow_open=None):
    """Прокрутка результата; allow_open — callable(index) для входа в запись."""
    idx = 0
    foot = "стрелки/j k — листать | x/Backspace — назад | q — выход"
    while True:
        cols, rows = term_size()
        budget = max(1, rows - 8)
        start = max(0, min(idx - budget // 2, max(0, len(lines) - budget)))
        visible = lines[start:start + budget]
        marked = []
        for i, text in enumerate(visible):
            cur = start + i
            marked.append((REV if cur == idx else "") + text[:cols - 2].ljust(cols - 2))
        render(title, marked, foot, "%d/%d" % (idx + 1, len(lines) or 1))
        key = read_key()
        if key in ("q",):
            return None
        if key in ("backspace", "x", "esc"):
            return None
        if key in ("up", "k"):
            idx = max(0, idx - 1)
        elif key in ("down", "j"):
            idx = min(max(0, len(lines) - 1), idx + 1)
        elif key == "enter" and allow_open and lines:
            return allow_open(idx + start)
        elif key.isdigit() and allow_open and lines:
            target = start + int(key) - 1
            if 0 <= target < len(lines):
                return allow_open(target)


def ask_line(prompt):
    """Однострочный ввод без сырого терминала."""
    cols, rows = term_size()
    buf = ""
    while True:
        render("ввод", [prompt, "", "› " + buf + "█"],
               "Enter — принять | Backspace — стирать | Esc — отмена", "")
        key = read_key()
        if key == "enter":
            return buf.strip()
        if key in ("esc", "backspace") and not buf:
            return None
        if key == "backspace":
            buf = buf[:-1]
        elif len(key) == 1 and key.isprintable():
            buf += key


# ---------------------------------------------------------------- общее: выбор организации
def org_summary():
    lines = []
    try:
        data = get("v2", "/v1/directory/organizations/%s" % org())
        lines += ["Организация: " + str(first(data, "name", "organizationName", default="?"))]
        lines += kv_lines(data or {}, limit=15)
    except ApiError as err:
        lines.append("ошибка: %s" % err)
    try:
        st = get("v2", "/v1/directory/organizations/%s/statistics" % org())
        lines.append("")
        lines.append("Статистика: " + json.dumps(st, ensure_ascii=False)[:400])
    except ApiError:
        pass
    return lines


# ---------------------------------------------------------------- 1. Пользователи
def user_card(uid, src="v2"):
    lines = []
    try:
        if src == "scim":
            data = get("scim", "/v2/Users/%s" % uid)
        else:
            data = get("v2", "/v1/directory/organizations/%s/users/%s" % (org(), uid))
        lines = kv_lines(data or {}, limit=40)
    except ApiError as err:
        lines = ["ошибка: %s" % err]
    try:
        al = get("v2", "/v1/api360/directory/org/%s/users/%s/aliases" % (org(), uid))
        lines.append("")
        vals = items_of(al, "aliases", "items") or []
        lines.append("Алиасы: " + (", ".join(str(v) for v in vals) if vals
                                   else "нет"))
    except ApiError as err:
        lines.append("алиасы: недоступны (%s)" % str(err)[:60])
    return lines


def menu_users():
    while True:
        act = menu("1. Пользователи",
                   [("список (v2 cloud-api)", users_list),
                    ("найти по нику/почте (v1)", users_search),
                    ("карточка по user_id", user_by_id),
                    ("SCIM: список записей passport", scim_list),
                    ("SCIM: запись по uid", scim_by_id),
                    ("статистика по пользователям", users_stats)],
                   "org=%s" % (org() or "?"))
        if act is None:
            return


def fetch_users_v2():
    out, offset, limit = [], 0, 50
    while True:
        data = get("v2", "/v1/directory/organizations/%s/users?limit=%d&offset=%d"
                   % (org(), limit, offset))
        batch = items_of(data, "users", "items", "Resources")
        out += batch
        if len(batch) < limit or len(out) > 500:
            break
        offset += limit
        time.sleep(PAUSE) if not MOCK else None
    return out


def users_list():
    try:
        users = fetch_users_v2()
    except ApiError as err:
        return show_paged("пользователи", ["ошибка: %s" % err])
    rows = [(u, "%s  %s  id=%s" % (cell(u, "userName", "login", "email").ljust(34),
                                   cell(u, "displayName", "name").ljust(26),
                                   cell(u, "id", "userId"))) for u in users]
    if not rows:
        return show_paged("пользователи", ["список пуст"])

    def open_(pos):
        if pos >= len(rows):
            return None
        uid = first(rows[pos][0], "id", "userId")
        show_paged("карточка %s" % uid, user_card(uid))
        return None
    show_paged("пользователи (%d) — Enter: карточка" % len(rows),
               [r[1] for r in rows], allow_open=open_)


def users_search():
    needle = ask_line("ник или почта (например ivanovaim):")
    if not needle:
        return
    params = {"nickname": needle} if "@" not in needle else {"email": needle}
    path = "/directory/v1/org/%s/users?%s" % (
        org(), urllib.parse.urlencode(params))
    try:
        data = get("v1", path)
    except ApiError as err:
        return show_paged("поиск v1", ["ошибка: %s" % err])
    found = items_of(data, "users", "items")
    lines = ["найдено: %d" % len(found)]
    for u in found:
        lines.append("id=%-10s nick=%-20s email=%s" % (
            cell(u, "id", "userId"), cell(u, "nickname"),
            cell(u, "email", "defaultEmail")))
    show_paged("поиск v1", lines)


def user_by_id():
    uid = ask_line("user_id:")
    if uid:
        show_paged("карточка %s" % uid, user_card(uid.strip()))


def users_stats():
    try:
        data = get("v2", "/v1/directory/organizations/%s/users/statistics" % org())
        show_paged("статистика пользователей", kv_lines(data or {}, 40))
    except ApiError as err:
        show_paged("статистика пользователей", ["ошибка: %s" % err])


def scim_list():
    try:
        data = get("scim", "/v2/Users?startIndex=1&count=100")
    except ApiError as err:
        return show_paged("SCIM Users", ["ошибка: %s" % err])
    res = items_of(data, "Resources")
    lines = ["totalResults=%s" % first(data, "totalResults", default="?")]
    for u in res:
        emails = ",".join(e.get("value", "") for e in u.get("emails", []))
        lines.append("id=%-22s %-32s %s" % (cell(u, "id")[:22],
                                            cell(u, "userName")[:32], emails[:40]))
    show_paged("SCIM Users (%d)" % len(res), lines)


def scim_by_id():
    uid = ask_line("SCIM uid:")
    if not uid:
        return
    try:
        data = get("scim", "/v2/Users/%s" % uid.strip())
        show_paged("SCIM %s" % uid, kv_lines(data or {}, 40))
    except ApiError as err:
        show_paged("SCIM %s" % uid, ["ошибка: %s" % err])


# ----------------------------- служебное: обёртка записи (write_guard)
def write_guard(desc, fn, skip_dry=False):
    """Единая точка записи: без --write показывает только предпросмотр."""
    if not WRITE:
        return "[read-only] шаг пропущен: %s (запусти с --write)" % desc
    if not confirm(desc):
        return "[отменено] " + desc
    time.sleep(0 if MOCK else PAUSE)
    try:
        fn()
        return "[ok] " + desc
    except ApiError as err:
        return "[ОШИБКА] %s: %s" % (desc, str(err)[:200])


# ---------------------------------------------------------------- 2. Отделы и группы
def menu_dirs():
    while True:
        act = menu("2. Отделы и группы",
                   [("отделы (v2)", lambda: simple_list("v2",
                        "/v1/directory/organizations/%s/departments" % org(),
                        "departments", ("id", "name", "headId"))),
                    ("отделы (v1 api360)", lambda: simple_list("v1",
                        "/directory/v1/org/%s/departments" % org(),
                        "departments", ("id", "name", "headId"))),
                    ("группы (v2)", lambda: simple_list("v2",
                        "/v1/directory/organizations/%s/groups" % org(),
                        "groups", ("id", "name", "email"))),
                    ("группы (v1)", lambda: simple_list("v1",
                        "/directory/v1/org/%s/groups" % org(),
                        "groups", ("id", "name", "email"))),
                    ("состав группы", group_members),
                    ("офисы", lambda: simple_list("v2",
                        "/v1/directory/organizations/%s/offices" % org(),
                        "offices", ("id", "name", "address"))),
                    ("комнаты", lambda: simple_list("v2",
                        "/v1/directory/organizations/%s/rooms" % org(),
                        "rooms", ("id", "name", "capacity"))),
                    ("внешние контакты (v1)", lambda: simple_list("v1",
                        "/directory/v1/org/%s/external_contacts" % org(),
                        "externalContacts", ("id", "name")))])
        if act is None:
            return


def simple_list(kind, path, *keys):
    try:
        data = get(kind, path)
    except ApiError as err:
        return show_paged(path.split("/")[-1], ["ошибка: %s" % err])
    rows = items_of(data, *(keys[0], "items", "Resources")) if keys else []
    lines = ["%s: %d записей" % (keys[0], len(rows))]
    for row in rows:
        lines.append("  ".join(cell(row, k) for k in keys[1]))
    show_paged(keys[0], lines or ["пусто"])


def group_members():
    gid = ask_line("group id:")
    if not gid:
        return
    try:
        data = get("v1", "/directory/v2/org/%s/groups/%s/members" % (org(), gid.strip()))
    except ApiError as err:
        return show_paged("группа %s" % gid, ["ошибка: %s" % err])
    rows = items_of(data, "members", "items")
    lines = ["участников: %d" % len(rows)]
    for m in rows:
        lines.append("%-12s %s" % (cell(m, "id", "userId"), cell(m, "name", "displayName", "type")))
    show_paged("группа %s" % gid, lines)


# ---------------------------------------------------------------- 3. Почта и домены
def menu_mail():
    while True:
        act = menu("3. Почта и домены",
                   [("домены организации (v1)", lambda: simple_list("v1",
                        "/directory/v1/org/%s/domains" % org(),
                        "domains", ("id", "verified", "status"))),
                    ("DKIM по домену", domain_dkim),
                    ("DNS-записи домена", domain_dns),
                    ("настройки почты (v2)", lambda: simple_list("v2",
                        "/v1/admin/org/%s/mail-settings" % org(),
                        "mail-settings", ())),
                    ("маршрутизация: поиск правил (v2)", mail_routes),
                    ("правила маршрутизации (v1)", lambda: simple_list("v1",
                        "/admin/v1/org/%s/mail/routing/rules" % org(),
                        "rules", ("id", "name", "enabled"))),
                    ("антиспам: allowlist IP (v1)", antispam_ips),
                    ("shared-ящики (v1)", lambda: simple_list("v1",
                        "/admin/v1/org/%s/mailboxes/shared" % org(),
                        "mailboxes", ("id", "email", "name"))),
                    ("делегированные (v1)", lambda: simple_list("v1",
                        "/admin/v1/org/%s/mailboxes/delegated" % org(),
                        "mailboxes", ("id", "email"))),
                    ("алиасы пользователя", aliases_menu)])
        if act is None:
            return


def domain_dkim():
    dom = ask_line("домен (romashka.ru):")
    if not dom:
        return
    try:
        data = get("v1", "/directory/v1/org/%s/domains/%s/dkim" % (org(), dom.strip()))
        show_paged("DKIM %s" % dom, kv_lines(data or {}, 30))
    except ApiError as err:
        show_paged("DKIM %s" % dom, ["ошибка: %s" % err])


def domain_dns():
    dom = ask_line("домен:")
    if not dom:
        return
    try:
        data = get("v1", "/directory/v1/org/%s/domains/%s/dns" % (org(), dom.strip()))
    except ApiError as err:
        return show_paged("DNS %s" % dom, ["ошибка: %s" % err])
    rows = items_of(data, "records", "dns", "items")
    lines = ["записей: %d" % len(rows)]
    for r in rows:
        lines.append("%-8s %-28s %s" % (cell(r, "recordType", "type"),
                                        cell(r, "name")[:28], cell(r, "value")[:40]))
    show_paged("DNS %s" % dom, lines)


def mail_routes():
    email = ask_line("почтовый адрес для поиска маршрутов:")
    if not email:
        return
    try:
        data = api("v2", "POST", "/v1/mail/organizations/%s/routes/search" % org(),
                   {"email": email.strip()})
        show_paged("маршруты %s" % email, kv_lines(data or {}, 30))
    except ApiError as err:
        show_paged("маршруты", ["ошибка: %s" % err])


def antispam_ips():
    try:
        data = get("v1", "/admin/v1/org/%s/mail/antispam/allowlist/ips" % org())
        show_paged("антиспам allowlist", kv_lines(data or {}, 40))
    except ApiError as err:
        show_paged("антиспам allowlist", ["ошибка: %s" % err])


def aliases_menu():
    uid = ask_line("user_id:")
    if not uid:
        return
    uid = uid.strip()
    base = "/v1/api360/directory/org/%s/users/%s/aliases" % (org(), uid)
    try:
        data = get("v2", base)
    except ApiError as err:
        return show_paged("алиасы %s" % uid, ["ошибка: %s" % err])
    rows = items_of(data, "aliases", "items")
    lines = ["алиасы user_id=%s: %d" % (uid, len(rows))] + [str(r) for r in rows]
    lines.append("")
    lines.append("Дальше: a — добавить алиас | d — удалить (только с --write)")
    show_paged("алиасы %s" % uid, lines)
    act = ask_line("действие (a/d/Enter — назад):")
    if act not in ("a", "d"):
        return
    alias = ask_line("алиас (локальная часть, без @домен):")
    if not alias:
        return
    base = "/v1/api360/directory/org/%s/users/%s/aliases" % (org(), uid)
    if act == "a":
        msg = write_guard("POST алиас %s" % alias, lambda: api(
            "v2", "POST", base, {"alias": alias.strip()}))
    else:
        msg = write_guard("DELETE алиас %s" % alias, lambda: api(
            "v2", "DELETE", base + "/" + urllib.parse.quote(alias.strip()),
            allow=(200, 204)))
    show_paged("алиасы", [msg])




# ---------------------------------------------------------------- 4. Аудит и статистика
def menu_audit():
    while True:
        act = menu("4. Аудит и статистика",
                   [("лентa событий (v2 auditlog)", audit_events),
                    ("фильтр: только Synchronization", lambda: audit_events("Synchronization")),
                    ("фильтр: Directory", lambda: audit_events("Directory")),
                    ("фильтр: Mail", lambda: audit_events("Mail")),
                    ("статистика организации", org_stats_view)])
        if act is None:
            return


def audit_events(service=None):
    params = {"limit": 50}
    if service:
        params["service"] = service
    try:
        data = get("v2", "/v1/auditlog/organizations/%s/events?%s"
                   % (org(), urllib.parse.urlencode(params)))
    except ApiError as err:
        return show_paged("аудит", ["ошибка: %s" % err])
    rows = items_of(data, "events", "items")
    lines = ["событий: %d (service=%s)" % (len(rows), service or "все")]
    for e in rows:
        actor = first(e, "actor.user", "actor.source", default="?")
        target = first(e, "target.user", "target.group", "target.domain", default="")
        lines.append("%s %-16s %-18s %s" % (cell(e, "timestamp")[:19],
                                            cell(e, "service")[:16],
                                            str(actor)[:18],
                                            (cell(e, "action") + " " + target)[:60]))
    show_paged("аудит событий", lines)


def org_stats_view():
    try:
        data = get("v2", "/v1/directory/organizations/%s/statistics" % org())
        show_paged("статистика", kv_lines(data or {}, 40))
    except ApiError as err:
        show_paged("статистика", ["ошибка: %s" % err])


# ---------------------------------------------------------------- 5. Утилиты / диагностика
def menu_util():
    while True:
        act = menu("5. Утилиты и диагностика",
                   [("конфигурация (токены замаскированы)", show_config),
                    ("сырой GET по пути из docs", raw_probe),
                    ("последние вызовы (лог сессии)", call_log),
                    ("проверка доступности хостов", probe_hosts),
                    ("самопроверка моков (selftest)", lambda: (
                        show_paged("selftest", selftest_lines())))])
        if act is None:
            return


def show_config():
    lines = ["ORG_ID: %s" % (org() or "(не задан)"),
             "SCIM domain_id: %s" % (ENV["Y360_SCIM_DOMAIN_ID"] or "(не задан)"),
             "токен v1/v2 (OAuth): %s" % mask(ENV["Y360_OAUTH_TOKEN"]),
             "токен v1 отдельный: %s" % mask(ENV.get("Y360_V1_TOKEN", "")),
             "токен v2 отдельный: %s" % mask(ENV.get("Y360_V2_TOKEN", "")),
             "токен SCIM (y0_): %s" % mask(ENV["Y360_SCIM_TOKEN"]),
             "", "хосты:", HOST_V1, HOST_V2,
             "https://{domain_id}.scim-api.passport.yandex.net",
             "", "режим: %s | mock: %s" % ("WRITE" if WRITE else "read-only",
                                            "да" if MOCK else "нет")]
    missing = [k for k in REQUIRED if not ENV.get(k)]
    if missing:
        lines += ["", "НЕ задано: " + ", ".join(missing)]
    show_paged("конфигурация", lines)


def raw_probe():
    path = ask_line("путь (например /v1/directory/organizations/{org}/users?limit=10):")
    if not path:
        return
    path = path.replace("{org}", str(org()))
    kind = ask_line("хост: v1 / v2 / scim:")
    if kind not in ("v1", "v2", "scim"):
        return
    try:
        data = get(kind, path)
        text = json.dumps(data, ensure_ascii=False, indent=1)
        show_paged("probe %s %s" % (kind, path[:40]), text.splitlines())
    except ApiError as err:
        show_paged("probe", ["ошибка: %s" % err])


def call_log():
    lines = ["вызовов за сессию: %d" % len(LOG)]
    lines += ["%s  %s" % (ts, msg) for ts, msg in LOG[-200:]]
    show_paged("лог вызовов", lines)


def probe_hosts():
    lines = []
    for name, url in (("api360", HOST_V1 + "/directory/v1/org/%s/domains" % org()),
                      ("cloud-api", HOST_V2 + "/v1/directory/organizations/%s" % org())):
        if MOCK:
            lines.append("%s: mock" % name)
            continue
        try:
            req = urllib.request.Request(url.split("?")[0], method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                lines.append("%s: HTTP %s" % (name, r.status))
        except urllib.error.HTTPError as err:
            lines.append("%s: HTTP %s (хост отвечает)" % (name, err.code))
        except Exception as exc:
            lines.append("%s: %s" % (name, str(exc)[:80]))
    show_paged("доступность", lines)


# ---------------------------------------------------------------- main / цикл
def menu(title, entries, state=""):
    """Одноуровневое меню: стрелки/цифры. Возвращает выбранное действие или None."""
    idx = 0
    while True:
        cols, rows = term_size()
        budget = max(1, rows - 8)
        lines = []
        for i, (label, _) in enumerate(entries):
            mark = ">" if i == idx else " "
            num = str(i + 1) if i < 9 else " "
            lines.append("%s [%s] %s" % (mark, num, label))
        render(title, lines,
               "↑↓/j k — выбор | цифры 1-9 | Enter — открыть | q — назад", state)
        key = read_key()
        if key in ("up", "k"):
            idx = (idx - 1) % len(entries)
        elif key in ("down", "j"):
            idx = (idx + 1) % len(entries)
        elif key.isdigit() and 1 <= int(key) <= len(entries):
            idx = int(key) - 1
            entries[idx][1]()
        elif key == "enter":
            entries[idx][1]()
        elif key in ("q", "esc", "backspace"):
            return None


def main():
    global MOCK, WRITE
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    load_env_file(".env")
    load_env_file(os.path.expanduser("~/.y360.env"))
    for cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),):
        load_env_file(cand)
    MOCK = "--mock" in args
    WRITE = "--write" in args
    if "--selftest" in args:
        print("\n".join(selftest_lines()))
        return 0 if "FAIL" not in "\n".join(selftest_lines()) else 1
    if "--check" in args:
        for k in REQUIRED:
            print("%s = %s" % (k, mask(ENV[k])))
        return 0
    if not MOCK:
        missing = [k for k in REQUIRED if not ENV.get(k)]
        if missing:
            sys.exit("не заданы переменные (положи .env рядом со скриптом): "
                     + ", ".join(missing))
    while True:
        act = menu("Яндекс 360 — %s%s" % ("MOCK" if MOCK else "ORG %s" % org(),
                                          " | WRITE" if WRITE else ""),
                   [("1. Пользователи и алиасы", menu_users),
                    ("2. Отделы, группы, офисы, комнаты", menu_dirs),
                    ("3. Почта, домены, DKIM, DNS", menu_mail),
                    ("4. Аудит-лог и статистика", menu_audit),
                    ("5. Утилиты и диагностика", menu_util),
                    ("6. Обзор организации", lambda: show_paged(
                        "обзор", org_summary()))],
                   "read-only" if not WRITE else "WRITE")
        if act is None:
            break
    sys.stdout.write(ESC + "[0m\n")
    return 0


def selftest_lines():
    global MOCK
    saved, MOCK = MOCK, True
    out = []
    try:
        users = items_of(get("scim", "/v2/Users?startIndex=1&count=100"), "Resources")
        out.append("mock scim list: %d" % len(users))
        card = get("v2", "/v1/directory/organizations/X/users/101")
        out.append("mock v2 card: ok")
        al = get("v2", "/v1/api360/directory/org/X/users/102/aliases")
        out.append("mock aliases: %s" % items_of(al, "aliases"))
        guard = write_guard("самопроверка read-only", lambda: None)
        out.append("read-only guard: %s" % ("OK" if "read-only" in guard else "FAIL"))
        out.append("mask: %s" % mask("y0_" + "x" * 40 + "abc"))
        out.append("patch schema: %s" % PATCH_SCHEMA)
        out.append("selftest: %d строк, ошибок нет" % len(out))
    except Exception as exc:
        out.append("FAIL selftest exception: %r" % exc)
    finally:
        MOCK = saved
    return out


if __name__ == "__main__":
    sys.exit(main())
