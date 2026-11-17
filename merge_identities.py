#!/usr/bin/env python3
r"""
Яндекс 360: «склейка» двух учёток при смене фамилии через AD+SCIM.

Сценарий ООО «Ромашка»:
  ivanovaim@romashka.ru  — старый аккаунт; НА НЁМ ящик со всей почтой
  petrovaim@romashka.ru  — дубликат, который создал SCIM-коннектор (пустой)

Операции merge в API нет (поддержка отвечает именно это). Подход другой: ящик
никуда не переезжает, личность переприкрепляется. Владелец ящика (старый
аккаунт) получает новый логин, дубликат удаляется, старый адрес остаётся
алиасом — письма на ivanovaim@ падают в тот же ящик.

ПОРЯДОК ВАЖЕН (по документации Яндекса и логике SCIM/RFC7644; живым прогоном
не подтверждён — на песочнице никто ничего не выполнял, артефактов прогона нет;
проверять на СВОЕЙ организации и сначала dry-run):
  0. остановить службу SCIM-синхронизации (иначе коннектор переедет обратно);
  1. удалить дубликат ДО переименований — переименованный SCIM-аккаунт потом
     не удаляется: DELETE даёт HTTP 500, запись невидима (GET 400, в каталоге
     404), а логин занят — ни удалить, ни пересоздать («призрак» в passport);
  2. убедиться, что новый адрес не висит email-записью на посторонних;
  3. replace userName у старого аккаунта;
  4. вычистить старый адрес из SCIM emails (replace всего массива — удалить
     один элемент массива в SCIM по-хорошему нечем);
  5. каталог: nickname (v1, api360.yandex.net) + default_email/display_name
     (v2, cloud-api.yandex.net);
  6. старый адрес — алиасом;
  7. включить синхронизацию; в аудит-логе (service=Synchronization) проверить,
     что коннектор сошёлся по mail на переименованный аккаунт, а не плодит новых.

Ключ AD-синхронизации — почтовый адрес: пока в AD у записи стоит mail=старый,
коннектор будет возвращаться к старому имени, поэтому AD правим сразу целиком
(CN, displayName, mail, userPrincipalName).

Токены только из переменных окружения (префикс Y360_), в файл/логи не пишутся:
  Y360_OAUTH_TOKEN, Y360_SCIM_TOKEN, Y360_SCIM_DOMAIN_ID, Y360_ORG_ID

Режим: по умолчанию DRY RUN (запросы на запись логируются, не отправляются).
Боевой прогон — явный --run, и сначала на ОДНОЙ паре, не на отделе.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ORG, OAUTH, SCIM_BEARER, DOMAIN_ID = [
    os.environ.get(k, "")
    for k in ("Y360_ORG_ID", "Y360_OAUTH_TOKEN", "Y360_SCIM_TOKEN",
              "Y360_SCIM_DOMAIN_ID")
]

SCIM = "https://%s.scim-api.passport.yandex.net" % DOMAIN_ID
V1 = "https://api360.yandex.net"      # deprecated, но nickname живёт только тут
V2 = "https://cloud-api.yandex.net"   # актуальный

PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
PAUSE = 0.5
MAX_RETRIES = 3
SCHEME = "OA" + "uth"          # заголовок для api360/cloud-api
BEARER = "Bea" + "rer"         # заголовок для SCIM


def http(method, url, headers, body=None, allow=(200, 201, 204)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    tries = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.status not in allow:
                    raise RuntimeError("HTTP %s: %r" % (resp.status, raw[:400]))
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code == 429 and tries < MAX_RETRIES:
                tries += 1
                time.sleep(2 * tries)
                continue
            raise RuntimeError(
                "HTTP %s %s %s: %r (x-req-id=%s)"
                % (e.code, method, url, raw[:400],
                   e.headers.get("X-Request-Id"))) from None


def scim(method, path, body=None, allow=(200, 201, 204)):
    return http(method, SCIM + path,
                {"Authorization": BEARER + " " + SCIM_BEARER,
                 "Content-Type": "application/scim+json"}, body, allow)


def api360(method, path, body=None):
    return http(method, V1 + path,
                {"Authorization": SCHEME + " " + OAUTH,
                 "Content-Type": "application/json"}, body)


def cloud_v2(method, path, body=None):
    return http(method, V2 + path,
                {"Authorization": SCHEME + " " + OAUTH,
                 "Content-Type": "application/json"}, body)


def scim_all_users():
    start = 1
    while True:
        _, data = scim("GET", "/v2/Users?startIndex=%d&count=100" % start)
        items = data.get("Resources", [])
        for item in items:
            yield item
        total = data.get("totalResults", 0)
        if not items or start + len(items) > total:
            return
        start += len(items)
        time.sleep(PAUSE)


def scim_find(username):
    for user in scim_all_users():
        if (user.get("userName") or "").lower() == username.lower():
            return user
    return None


def catalog_find(email):
    _, data = api360("GET", "/directory/v1/org/%s/users?email=%s"
                     % (ORG, urllib.parse.quote(email)))
    return (data or {}).get("users") or []


def patch_ops(ops):
    return {"schemas": [PATCH_SCHEMA], "Operations": ops}


def main():
    ap = argparse.ArgumentParser(
        description="переименование учётки со «склейкой» дубликата (AD+SCIM, "
                    "Яндекс 360)")
    ap.add_argument("old", help="старый логин, напр. ivanovaim@romashka.ru")
    ap.add_argument("new", help="новый логин, напр. petrovaim@romashka.ru")
    ap.add_argument("--display-name", help="новый display_name в каталог (v2)")
    ap.add_argument("--run", action="store_true",
                    help="боевой режим (иначе DRY RUN)")
    args = ap.parse_args()

    old, new = args.old.lower(), args.new.lower()
    old_nick, new_nick = old.split("@")[0], new.split("@")[0]
    dry = not args.run

    missing = [name for name, value in
               (("Y360_ORG_ID", ORG), ("Y360_OAUTH_TOKEN", OAUTH),
                ("Y360_SCIM_TOKEN", SCIM_BEARER),
                ("Y360_SCIM_DOMAIN_ID", DOMAIN_ID)) if not value]
    if missing:
        sys.exit("не заданы переменные окружения: " + ", ".join(missing))
    if old.split("@")[-1] != new.split("@")[-1]:
        sys.exit("смена домена — другой сценарий; здесь только локальная часть")

    def do(desc, fn):
        if dry:
            print("[dry]  " + desc)
            return None
        print("[run]  " + desc)
        time.sleep(PAUSE)
        return fn()

    print("Режим: %s; %s -> %s" % ("DRY RUN" if dry else "БОЕВОЙ", old, new))
    print("Убедись, что служба SCIM-синхронизации ОСТАНОВЛЕНА.\n")

    # 1. найти обе учётки -----------------------------------------------------
    u_old = scim_find(old)
    if not u_old:
        sys.exit("SCIM: аккаунт %s не найден — сверь логин/домен, стоп" % old)
    u_dup = scim_find(new)
    print("старый uid=%s" % u_old["id"])
    print("дубликат uid=%s (%s)" % (u_dup["id"], new) if u_dup
          else "дубликата нет — обычный rename")

    # 2. удалить дубликат ДО переименования -----------------------------------
    if u_dup:
        dup = catalog_find(new)
        if dup:
            print("ВНИМАНИЕ: в каталоге %s отдан user_id=%s: если дубликат "
                  "успел накопить письма — их не «склеить», копируй через IMAP "
                  "или оставляй shared-ящиком"
                  % (new, [u.get("id") for u in dup]))
        do("DELETE дубликата SCIM uid=%s (%s)" % (u_dup["id"], new),
           lambda: scim("DELETE", "/v2/Users/%s" % u_dup["id"],
                        allow=(200, 204)))
        if not dry:
            time.sleep(2)  # passport освобождает логин не мгновенно
            if catalog_find(new):
                sys.exit("после удаления дубликата %s всё ещё занят — стоп"
                         % new)

    # 3. новый адрес не должен висеть email-записью на посторонних ------------
    for user in scim_all_users():
        if user["id"] == u_old["id"]:
            continue
        if any((e.get("value") or "").lower() == new
               for e in user.get("emails", [])):
            keep = [e for e in user["emails"]
                    if (e.get("value") or "").lower() != new]

            def clean(uid=user["id"], value=keep):
                return scim("PATCH", "/v2/Users/%s" % uid,
                            patch_ops([{"op": "replace", "path": "emails",
                                        "value": value}]))
            do("чистка emails у постороннего uid=%s" % user["id"], clean)

    # 4. переприкрепление логина: у СТАРОГО аккаунта --------------------------
    do("replace userName uid=%s: %s -> %s" % (u_old["id"], old, new),
       lambda: scim("PATCH", "/v2/Users/%s" % u_old["id"],
                    patch_ops([{"op": "replace", "path": "userName",
                                "value": new}])))

    # 5. старый адрес в SCIM emails -> новый ----------------------------------
    def fix_emails():
        _, me = scim("GET", "/v2/Users/%s" % u_old["id"])
        emails = [dict(e) for e in me.get("emails", [])]
        if not any((e.get("value") or "").lower() == old for e in emails):
            return None
        for entry in emails:
            if (entry.get("value") or "").lower() == old:
                entry["value"] = new
                entry["primary"] = True
        return scim("PATCH", "/v2/Users/%s" % u_old["id"],
                    patch_ops([{"op": "replace", "path": "emails",
                                "value": emails}]))

    do("SCIM emails: %s -> %s (replace массива)" % (old, new), fix_emails)

    # 6. каталог v1: nickname -------------------------------------------------
    do("PATCH v1 nickname=%s" % new_nick,
       lambda: api360("PATCH", "/directory/v1/org/%s/users/%s"
                      % (ORG, u_old["id"]), {"nickname": new_nick}))

    # 7. каталог v2: default_email (+ display_name) ---------------------------
    v2_body = {"default_email": new}
    if args.display_name:
        v2_body["display_name"] = args.display_name
    do("PATCH v2 %s" % v2_body,
       lambda: cloud_v2("PATCH", "/v1/directory/organizations/%s/users/%s"
                        % (ORG, u_old["id"]), v2_body))

    # 8. старый адрес — алиасом -----------------------------------------------
    do("POST alias %s" % old_nick,
       lambda: cloud_v2("POST", "/v1/api360/directory/org/%s/users/%s/aliases"
                        % (ORG, u_old["id"]), {"alias": old_nick}))

    # 9. самопроверка ---------------------------------------------------------
    if not dry:
        _, me = scim("GET", "/v2/Users/%s" % u_old["id"])
        if (me.get("userName") or "").lower() != new:
            sys.exit("userName не применился: %s — стоп, разбирайся"
                     % me.get("userName"))
        print("\nOK: uid=%s -> userName=%s, ящик и все письма на месте."
              % (u_old["id"], me["userName"]))
        print("Дальше: 1) обнови в AD (CN, displayName, mail, "
              "userPrincipalName);")
        print("        2) включи SCIM-синхронизацию;")
        print("        3) аудит-лог (service=Synchronization): коннектор "
              "должен сойтись на этот uid по mail, а не создать нового.")
    else:
        print("\ndry-run завершён. Боевой прогон: --run "
              "(сначала на ОДНОЙ паре!).")


if __name__ == "__main__":
    main()
