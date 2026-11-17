#!/usr/bin/env python3
r"""
y360_gui.py — graphical interface (Windows/Linux/macOS) for managing a
Yandex 360 organization. Tkinter is part of the Python standard library;
on Windows nothing extra is needed (the python.org build includes Tcl/Tk).

Usage:
  python y360_gui.py            — read .env next to the script, read-only
  python y360_gui.py --mock     — demo data of the fictional "Romashka", network untouched
  python y360_gui.py --env C:\path\.env
  python y360_gui.py --selftest-gui   — smoke test without showing a window (xvfb/headless)

The API layer (env, masks, retries, mock data, endpoints) is reused from
y360_tui.py — both files must sit side by side. Writes are disabled by
default; enable them with the "Write mode" checkbox — every
action then asks for confirmation. Token values are never shown, only masks.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import y360_tui as core

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError as exc:
    sys.exit("tkinter required: %s\nWindows: install Python from python.org "
             "(with the tcl/tk option), Linux: apt install python3-tk" % exc)

APP_TITLE = "Yandex 360 — admin console"
SERVICES = ["(all)", "Synchronization", "Directory", "Mail", "Disk",
            "Portal", "Calendar"]


# ---------------------------------------------------------------- helpers
def jdump(data):
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def rows_of(data, *list_keys):
    return core.items_of(data, *list_keys) if isinstance(data, (dict, list)) else []


# ---------------------------------------------------------------- application
class App:
    def __init__(self, root, mock=False, env_file=None):
        self.root = root
        self.q = queue.Queue()
        self.write_mode = tk.BooleanVar(value=False)
        self.busy_count = 0
        if mock:
            core.MOCK = True
        root.title(APP_TITLE + (" — MOCK" if core.MOCK else ""))
        root.geometry("1120x720")
        root.minsize(880, 560)
        self.build_statusbar()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.tab_users = UsersTab(self)
        self.tab_dirs = DirsTab(self)
        self.tab_mail = MailTab(self)
        self.tab_audit = AuditTab(self)
        self.tab_util = UtilTab(self)
        self.build_menu()
        for tab, title in ((self.tab_users, "Users"),
                           (self.tab_dirs, "Departments and groups"),
                           (self.tab_mail, "Mail and domains"),
                           (self.tab_audit, "Audit"),
                           (self.tab_util, "Utilities")):
            self.nb.add(tab, text=title)
        core.WRITE = False
        self.poll()

    # -- menu/status
    def build_menu(self):
        menubar = tk.Menu(self.root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Reload .env", command=self.reload_env)
        m_file.add_command(label="Configuration (masks)…",
                           command=self.tab_util.show_config)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.root.destroy)
        m_mode = tk.Menu(menubar, tearoff=0)
        m_mode.add_checkbutton(label="Write mode (--write)",
                               variable=self.write_mode,
                               command=self.mode_changed)
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="About", command=self.about)
        menubar.add_cascade(label="File", menu=m_file)
        menubar.add_cascade(label="Mode", menu=m_mode)
        menubar.add_cascade(label="Help", menu=m_help)
        self.root.config(menu=menubar)

    def build_statusbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom", padx=6, pady=4)
        self.mode_lbl = ttk.Label(bar, text="READ-ONLY", foreground="#0a7d00")
        self.mode_lbl.pack(side="left")
        ttk.Label(bar, text="  |  org=%s  |  " % (core.org() or "?")).pack(side="left")
        self.busy_lbl = ttk.Label(bar, text="ready")
        self.busy_lbl.pack(side="left")
        self.status_lbl = ttk.Label(bar, text="", anchor="e")
        self.status_lbl.pack(side="right", fill="x", expand=True)

    def mode_changed(self):
        core.WRITE = self.write_mode.get()
        if core.WRITE:
            self.mode_lbl.config(text="WRITE ENABLED", foreground="#b00020")
            self.status("Write mode enabled. Every action will ask "
                        "for confirmation.")
        else:
            self.mode_lbl.config(text="READ-ONLY", foreground="#0a7d00")
            self.status("Write mode disabled.")

    def about(self):
        messagebox.showinfo(
            "About",
            "%s\n%s\n\nAPI layer: y360_tui.py (stdlib+urllib)\n"
            "Hosts:\n  v1 %s (deprecated, nickname)\n  v2 %s\n"
            "  SCIM https://{domain_id}.scim-api.passport.yandex.net\n\n"
            "Tokens: only from .env/environment; shown as masks."
            % (APP_TITLE, core.APP, core.HOST_V1, core.HOST_V2))

    def reload_env(self):
        path = filedialog.askopenfilename(
            title=".env file", filetypes=[("env", ".env*"), ("all", "*.*")])
        if path and core.load_env_file(path):
            self.status(".env loaded: %s" % os.path.basename(path))
            self.tab_util.show_config()
        elif path:
            messagebox.showwarning(".env", "File not found/unreadable")

    def status(self, text):
        self.status_lbl.config(text=text[:160])

    # -- background API calls
    def call(self, kind, method, path, body=None, done=None,
             allow=(200, 201, 204), what=None):
        """Non-blocking request; the result arrives via the queue and done(data)."""
        self.busy_count += 1
        self.busy_lbl.config(text="request: %s" % (what or path[:60]))

        def worker():
            try:
                out = core.api(kind, method, path, body, allow=allow)
                err = None
            except core.ApiError as exc:
                out, err = None, str(exc)
            except Exception as exc:
                out, err = None, "%s: %s" % (type(exc).__name__, exc)
            self.q.put((done, out, err, what or "%s %s" % (method, path)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                done, out, err, what = self.q.get_nowait()
                self.busy_count = max(0, self.busy_count - 1)
                if self.busy_count == 0:
                    self.busy_lbl.config(text="ready")
                if err:
                    self.status("%s → ERROR: %s" % (what, err))
                    if done:
                        done(None, err)
                else:
                    self.status("%s → ok" % what)
                    if done:
                        done(out, None)
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def guard_write(self, desc, fn):
        """Writes only in write mode and with explicit confirmation."""
        if not self.write_mode.get():
            messagebox.showinfo(
                "Read-only",
                "Action blocked (read-only mode):\n%s\n\n"
                "Enable Mode → Write mode if you are sure." % desc)
            return False
        if not messagebox.askyesno("Write confirmation", desc,
                                   icon="warning", default="no"):
            self.status("cancelled: %s" % desc)
            return False
        fn()
        return True


# ---------------------------------------------------------------- base tab
class Tab(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.nb)
        self.app = app

    def output_pane(self, master, height=18):
        txt = tk.Text(master, wrap="word", height=height, font=("Consolas", 10))
        txt.pack(fill="both", expand=True)
        return txt

    def put(self, txt, data, err=None):
        txt.config(state="normal")
        txt.delete("1.0", "end")
        if err:
            txt.insert("end", "ERROR: %s\n" % err)
        elif data is None:
            txt.insert("end", "(empty response)\n")
        else:
            txt.insert("end", jdump(data) + "\n")
        txt.config(state="disabled")


def run_bg(app, fn, done, what):
    """Run an arbitrary synchronous function (a series of requests) in a background thread."""
    app.busy_count += 1
    app.busy_lbl.config(text=what[:60])

    def worker():
        try:
            out, err = fn(), None
        except core.ApiError as exc:
            out, err = None, str(exc)
        except Exception as exc:
            out, err = None, "%s: %s" % (type(exc).__name__, exc)
        app.q.put((done, out, err, what))
    threading.Thread(target=worker, daemon=True).start()


# ================================================================ USERS
class UsersTab(Tab):
    def __init__(self, app):
        super().__init__(app)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Button(top, text="Refresh (v2)", command=self.refresh).pack(side="left")
        ttk.Label(top, text="  Search (nickname/email, v1):").pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.search_var, width=26)
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self.search())
        ttk.Button(top, text="Find", command=self.search).pack(side="left")
        ttk.Button(top, text="Card v2", command=lambda: self.card("v2")
                   ).pack(side="left", padx=(14, 0))
        ttk.Button(top, text="Card SCIM", command=lambda: self.card("scim")
                   ).pack(side="left")
        ttk.Button(top, text="Aliases", command=self.aliases).pack(side="left")
        ttk.Button(top, text="Statistics", command=self.stats).pack(side="left")

        mid = ttk.Panedwindow(self, orient="vertical")
        mid.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        frame = ttk.Frame(mid)
        mid.add(frame, weight=3)
        cols = ("id", "userName", "displayName")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="browse")
        for cid, title, w in (("id", "user_id", 160),
                              ("userName", "login", 280),
                              ("displayName", "name", 240)):
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=w, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.card("v2"))
        self.out = self.output_pane(mid)
        mid.add(self.out, weight=2)
        self.uid = None

    def on_select(self, _event=None):
        sel = self.tree.selection()
        self.uid = str(self.tree.item(sel[0], "values")[0]) if sel else None

    def refresh(self):
        def done(data, err):
            self.tree.delete(*self.tree.get_children())
            if err:
                self.put(self.out, None, err)
                return
            users = data or []
            for u in users:
                self.tree.insert("", "end", values=(
                    core.first(u, "id", "userId", default="?"),
                    core.first(u, "userName", "login", "email", default=""),
                    core.first(u, "displayName", "name", default="")))
            self.app.status("users: %d" % len(users))
            self.put(self.out, {"loaded": len(users),
                                "hint": "double click — v2 card"})
        run_bg(self.app, core.fetch_users_v2, done, "v2 user list")

    def search(self):
        needle = self.search_var.get().strip()
        if not needle:
            return
        params = ({"nickname": needle} if "@" not in needle
                  else {"email": needle})
        path = "/directory/v1/org/%s/users?%s" % (
            core.org(), urllib.parse.urlencode(params))

        self.app.call("v1", "GET", path, done=lambda d, e: self.put(
            self.out, d, e), what="search v1 %s" % needle)

    def _uid_or_ask(self):
        if self.uid:
            return self.uid
        from tkinter import simpledialog
        val = simpledialog.askstring("user_id", "Enter user_id:",
                                     parent=self)
        return str(val).strip() if val else None

    def card(self, src):
        uid = self._uid_or_ask()
        if not uid:
            return

        def work():
            return "\n".join(core.user_card(uid, src=src))
        run_bg(self.app, work,
               lambda d, e: self._put_text(d, e),
               "card %s %s" % (src, uid))

    def _put_text(self, text, err):
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", ("ERROR: %s\n" % err) if err else (text or ""))
        self.out.config(state="disabled")

    def aliases(self):
        uid = self._uid_or_ask()
        if not uid:
            return
        self.app.call(
            "v2", "GET",
            "/v1/api360/directory/org/%s/users/%s/aliases" % (core.org(), uid),
            done=lambda d, e: self.put(self.out, d, e),
            what="aliases %s" % uid)

    def stats(self):
        self.app.call(
            "v2", "GET",
            "/v1/directory/organizations/%s/users/statistics" % core.org(),
            done=lambda d, e: self.put(self.out, d, e),
            what="user statistics")


# ================================================================ DEPARTMENTS/GROUPS
class DirsTab(Tab):
    """Generic tab: endpoint dropdown + button."""
    ENDPOINTS = [
        ("departments (v2)", "v2", "/v1/directory/organizations/%s/departments"),
        ("departments (v1 api360)", "v1", "/directory/v1/org/%s/departments"),
        ("groups (v2)", "v2", "/v1/directory/organizations/%s/groups"),
        ("groups (v1)", "v1", "/directory/v1/org/%s/groups"),
        ("offices (v2)", "v2", "/v1/directory/organizations/%s/offices"),
        ("rooms (v2)", "v2", "/v1/directory/organizations/%s/rooms"),
        ("external contacts (v1)", "v1",
         "/directory/v1/org/%s/external_contacts"),
        ("organization logo (v2)", "v2",
         "/v1/directory/organizations/%s/logo"),
    ]

    def __init__(self, app):
        super().__init__(app)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        self.combo = ttk.Combobox(top, state="readonly", width=34,
                                  values=[e[0] for e in self.ENDPOINTS])
        self.combo.current(0)
        self.combo.pack(side="left")
        ttk.Button(top, text="Show", command=self.fetch).pack(side="left",
                                                                  padx=6)
        ttk.Button(top, text="Group members (gid)…",
                   command=self.members).pack(side="left", padx=(14, 0))
        self.gid_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.gid_var, width=10).pack(side="left",
                                                                 padx=4)
        self.out = self.output_pane(self, height=30)
        self.out.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def fetch(self):
        label, kind, path = self.ENDPOINTS[self.combo.current()]
        path = path % core.org()
        self.app.call(kind, "GET", path,
                      done=lambda d, e: self.put(self.out, d, e),
                      what=label)

    def members(self):
        gid = self.gid_var.get().strip()
        if not gid:
            messagebox.showinfo("Group", "Enter group id next to the button")
            return
        path = "/directory/v2/org/%s/groups/%s/members" % (core.org(), gid)
        self.app.call("v1", "GET", path,
                      done=lambda d, e: self.put(self.out, d, e),
                      what="group members %s" % gid)


# ================================================================ MAIL
class MailTab(Tab):
    def __init__(self, app):
        super().__init__(app)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        for text, cmd in (("domains", self.domains),
                          ("DKIM…", self.dkim), ("DNS…", self.dns),
                          ("mail-settings", self.mail_settings),
                          ("routes…", self.routes),
                          ("rules (v1)", self.routing_rules),
                          ("antispam allowlist", self.antispam),
                          ("shared mailboxes", self.shared),
                          ("delegated", self.delegated),
                          ("aliases (uid)…", self.aliases)):
            ttk.Button(top, text=text, command=cmd).pack(side="left", padx=2)
        ask = ttk.Frame(self)
        ask.pack(fill="x", padx=6)
        ttk.Label(ask, text="argument (domain / email / uid):").pack(side="left")
        self.arg_var = tk.StringVar()
        ttk.Entry(ask, textvariable=self.arg_var, width=34).pack(side="left",
                                                                 padx=4)
        self.out = self.output_pane(self, height=30)
        self.out.pack(fill="both", expand=True, padx=6, pady=6)

    def _arg(self, hint):
        val = self.arg_var.get().strip()
        if not val:
            messagebox.showinfo("Argument required",
                                "Enter %s in the argument field" % hint)
            return None
        return val

    def domains(self):
        self.app.call("v1", "GET", "/directory/v1/org/%s/domains" % core.org(),
                      done=lambda d, e: self.put(self.out, d, e),
                      what="domains")

    def dkim(self):
        dom = self._arg("domain (romashka.ru)")
        if dom:
            self.app.call("v1", "GET",
                          "/directory/v1/org/%s/domains/%s/dkim"
                          % (core.org(), urllib.parse.quote(dom)),
                          done=lambda d, e: self.put(self.out, d, e),
                          what="DKIM %s" % dom)

    def dns(self):
        dom = self._arg("domain")
        if dom:
            self.app.call("v1", "GET",
                          "/directory/v1/org/%s/domains/%s/dns"
                          % (core.org(), urllib.parse.quote(dom)),
                          done=lambda d, e: self.put(self.out, d, e),
                          what="DNS %s" % dom)

    def mail_settings(self):
        self.app.call("v2", "GET",
                      "/v1/admin/org/%s/mail-settings" % core.org(),
                      done=lambda d, e: self.put(self.out, d, e),
                      what="mail-settings")

    def routes(self):
        email = self._arg("email address")
        if email:
            self.app.call("v2", "POST",
                          "/v1/mail/organizations/%s/routes/search"
                          % core.org(), {"email": email},
                          done=lambda d, e: self.put(self.out, d, e),
                          what="routes %s" % email)

    def routing_rules(self):
        self.app.call("v1", "GET",
                      "/admin/v1/org/%s/mail/routing/rules" % core.org(),
                      done=lambda d, e: self.put(self.out, d, e),
                      what="routing rules")

    def antispam(self):
        self.app.call("v1", "GET",
                      "/admin/v1/org/%s/mail/antispam/allowlist/ips"
                      % core.org(),
                      done=lambda d, e: self.put(self.out, d, e),
                      what="antispam allowlist")

    def shared(self):
        self.app.call("v1", "GET",
                      "/admin/v1/org/%s/mailboxes/shared" % core.org(),
                      done=lambda d, e: self.put(self.out, d, e),
                      what="shared mailboxes")

    def delegated(self):
        self.app.call("v1", "GET",
                      "/admin/v1/org/%s/mailboxes/delegated" % core.org(),
                      done=lambda d, e: self.put(self.out, d, e),
                      what="delegated")

    def aliases(self):
        uid = self._arg("user_id")
        if not uid:
            return
        base = "/v1/api360/directory/org/%s/users/%s/aliases" % (core.org(),
                                                                 uid)
        self.app.call("v2", "GET", base,
                      done=lambda d, e: self.put(self.out, d, e),
                      what="aliases %s" % uid)


# ================================================================ AUDIT
class AuditTab(Tab):
    def __init__(self, app):
        super().__init__(app)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="service:").pack(side="left")
        self.svc = ttk.Combobox(top, state="readonly", width=16,
                                values=SERVICES)
        self.svc.current(0)
        self.svc.pack(side="left", padx=4)
        ttk.Label(top, text="limit:").pack(side="left")
        self.lim_var = tk.StringVar(value="50")
        ttk.Entry(top, textvariable=self.lim_var, width=6).pack(side="left",
                                                                padx=4)
        ttk.Button(top, text="Fetch events", command=self.fetch).pack(
            side="left", padx=6)
        ttk.Button(top, text="Synchronization only",
                   command=lambda: self.fetch("Synchronization")).pack(
            side="left")
        ttk.Button(top, text="Organization statistics",
                   command=self.stats).pack(side="left", padx=14)
        cols = ("ts", "service", "actor", "action")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for cid, title, w in (("ts", "time", 150), ("service", "service", 120),
                              ("actor", "actor", 160),
                              ("action", "action/target", 420)):
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=w, anchor="w")
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0),
                       pady=6)
        vsb.pack(side="left", fill="y", pady=6)
        self.tree.bind("<Double-1>", self.on_dbl)
        self.events = []

    def fetch(self, service=None):
        if service is None:
            sel = self.svc.get()
            service = None if sel == "(all)" else sel
        params = {"limit": self.lim_var.get().strip() or "50"}
        if service:
            params["service"] = service
        path = "/v1/auditlog/organizations/%s/events?%s" % (
            core.org(), urllib.parse.urlencode(params))

        def done(data, err):
            self.tree.delete(*self.tree.get_children())
            self.events = rows_of(data, "events", "items") if not err else []
            for ev in self.events:
                actor = core.first(ev, "actor.user", "actor.source",
                                   default="?")
                target = core.first(ev, "target.user", "target.group",
                                    "target.domain", default="")
                self.tree.insert("", "end", values=(
                    str(core.first(ev, "timestamp", default=""))[:19],
                    core.first(ev, "service", default=""),
                    str(actor)[:24],
                    (str(core.first(ev, "action", default="")) + " "
                     + str(target))[:110]))
            self.app.status("audit: %d events (service=%s)"
                            % (len(self.events), service or "all"))
        self.app.call("v2", "GET", path, done=done,
                      what="audit service=%s" % (service or "all"))

    def on_dbl(self, _event):
        sel = self.tree.selection()
        if sel:
            idx = self.tree.index(sel[0])
            if idx < len(self.events):
                messagebox.showinfo("Event", jdump(self.events[idx]))

    def stats(self):
        self.app.call("v2", "GET",
                      "/v1/directory/organizations/%s/statistics" % core.org(),
                      done=lambda d, e: messagebox.showinfo(
                          "Statistics", jdump(d) if not e else "ERROR: %s" % e),
                      what="organization statistics")


# ================================================================ UTILITIES
class UtilTab(Tab):
    def __init__(self, app):
        super().__init__(app)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        for text, cmd in (("Configuration (masks)", self.show_config),
                          ("Session call log", self.show_log),
                          ("Organization overview", self.org_overview)):
            ttk.Button(top, text=text, command=cmd).pack(side="left", padx=2)
        ask = ttk.Frame(self)
        ask.pack(fill="x", padx=6)
        ttk.Label(ask, text="raw GET (kind, path):").pack(side="left")
        self.kind = ttk.Combobox(ask, state="readonly", width=6,
                                 values=("v1", "v2", "scim"))
        self.kind.current(1)
        self.kind.pack(side="left", padx=4)
        self.path_var = tk.StringVar(
            value="/v1/directory/organizations/ORG_ID/departments")
        ttk.Entry(ask, textvariable=self.path_var, width=60).pack(side="left",
                                                                  padx=4)
        ttk.Button(ask, text="GET", command=self.raw_get).pack(side="left")
        self.out = self.output_pane(self, height=28)
        self.out.pack(fill="both", expand=True, padx=6, pady=6)

    def show_config(self, _=None):
        lines = ["Configuration (token values are not shown):", ""]
        for key in core.REQUIRED + core.OPTIONAL:
            lines.append("  %-22s = %s" % (key, core.mask(core.ENV.get(key,
                                                                        ""))))
        lines += ["",
                  "  hosts: v1 %s" % core.HOST_V1,
                  "         v2 %s" % core.HOST_V2,
                  "         scim https://%s.scim-api.passport.yandex.net"
                  % (core.ENV.get("Y360_SCIM_DOMAIN_ID") or "{domain_id}"),
                  "  MOCK=%s  WRITE=%s" % (core.MOCK, core.WRITE)]
        self._put_lines(lines)

    def show_log(self):
        lines = ["Session call log (%d):" % len(core.LOG), ""]
        lines += ["  %s  %s" % row for row in core.LOG[-200:]]
        self._put_lines(lines)

    def org_overview(self):
        run_bg(self.app, core.org_summary,
               lambda d, e: self._put_lines((d or "").splitlines())
               if not e else self.put(self.out, None, e),
               "organization overview")

    def raw_get(self):
        path = self.path_var.get().strip().replace("ORG_ID", core.org() or "")
        if not path:
            return
        self.app.call(self.kind.get(), "GET", path,
                      done=lambda d, e: self.put(self.out, d, e),
                      what="raw %s %s" % (self.kind.get(), path))

    def _put_lines(self, lines):
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", "\n".join(lines) + "\n")
        self.out.config(state="disabled")


# ================================================================ main
def selftest_gui():
    """Smoke test without showing a window: build App and exercise the key paths."""
    import tempfile
    core.MOCK = True
    core.ENV.update({"Y360_ORG_ID": "1000",
                     "Y360_SCIM_DOMAIN_ID": "1234567"})
    # token-bearing env keys get values only in process memory
    core.ENV["Y360_OAUTH_TOKEN"] = "test-oauth-0123456789"
    core.ENV["Y360_SCIM_TOKEN"] = "y0_test-scim-0123456789"
    root = tk.Tk()
    root.withdraw()
    app = App(root, mock=True)
    ok = []
    # 1. mock user data loads synchronously (MOCK — the network stays silent)
    users = core.fetch_users_v2()
    ok.append(("fetch_users_v2 mock: %d" % len(users), len(users) >= 4))
    # 2. writes are blocked without --write
    guard = core.write_guard("read-only selftest", lambda: None)
    ok.append(("read-only guard", "read-only" in guard))
    # 3. masking
    ok.append(("mask does not print the token",
               "test-oauth" not in core.mask(core.ENV["Y360_OAUTH_TOKEN"])))
    # 4. tabs are created
    ok.append(("all tabs present",
               all(t is not None for t in (app.tab_users,
                                           app.tab_dirs, app.tab_mail,
                                           app.tab_audit,
                                           app.tab_util))))
    root.update_idletasks()
    root.destroy()
    fails = [name for name, good in ok if not good]
    for name, good in ok:
        print("%s %s" % ("ok  " if good else "FAIL", name))
    print("selftest-gui: %d checks, %d failures" % (len(ok), len(fails)))
    return 1 if fails else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--env", help="path to .env (default: next to "
                                      "the script)")
    parser.add_argument("--mock", action="store_true",
                        help="demo data of the fictional Romashka, no network")
    parser.add_argument("--write", action="store_true",
                        help="allow writes (each with confirmation); "
                             "in the GUI also toggled via the Mode menu")
    parser.add_argument("--check", action="store_true",
                        help="print the loaded config (masked) and exit")
    parser.add_argument("--selftest-gui", action="store_true",
                        help="GUI smoke test without showing a window")
    args = parser.parse_args(argv)

    env_path = args.env or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env")
    if not args.env:
        home_env = os.path.expanduser("~/.y360.env")
        core.load_env_file(env_path)
        core.load_env_file(home_env)
    else:
        if not core.load_env_file(args.env):
            sys.exit("failed to read %s" % args.env)

    if args.mock:
        core.MOCK = True
    core.WRITE = args.write

    missing = [k for k in core.REQUIRED if not core.ENV.get(k)]
    if missing and not core.MOCK:
        sys.exit("not set in .env: %s\n(example: y360.env.example next "
                 "to the script)" % ", ".join(missing))

    if args.selftest_gui:
        return selftest_gui()
    if args.check:
        for key in core.REQUIRED + core.OPTIONAL:
            print("%s = %s" % (key, core.mask(core.ENV.get(key, ""))))
        return 0

    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass
    App(root, mock=args.mock, env_file=args.env)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
