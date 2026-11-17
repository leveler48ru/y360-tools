# y360-tools — Yandex 360 administrator tools

A set of pure Python 3 scripts (standard library only) for managing a
Yandex 360 organization via REST API (Directory, Mail, Audit) and SCIM.

## Contents

| File | Purpose |
|---|---|
| `y360_tui.py` | Text-mode console (ANSI terminal): users, departments, groups, mail, audit log |
| `y360_gui.py` | The same functionality as a GUI (Tkinter, Windows/Linux/macOS). The API layer is reused from `y360_tui.py` — both files must sit side by side |
| `y360.env.example` | Configuration template |

## Setup

```bash
cp y360.env.example .env   # then fill in the values
```

- `Y360_ORG_ID` — numeric organization id
- `Y360_OAUTH_TOKEN` — OAuth token of an application with Directory/Mail permissions
- `Y360_SCIM_TOKEN` — SCIM connector Bearer token (`y0_...` format)
- `Y360_SCIM_DOMAIN_ID` — domain_id for the host `https://{domain_id}.scim-api.passport.yandex.net`
- optional: `Y360_V1_TOKEN` / `Y360_V2_TOKEN` (separate tokens for v1/v2), `Y360_TIMEOUT`

Tokens are read only from the environment / `.env`; on screen and in logs
they appear only in masked form like `y0_…abc`. `.env` never gets committed
to the repository.

## Usage

```bash
python3 y360_tui.py --mock      # demo data of a fictional organization, no network access
python3 y360_tui.py --check     # print the masked configuration
python3 y360_tui.py             # read-only: GET only, writes are blocked
python3 y360_tui.py --write     # allow writes (every step asks for confirmation)
python3 y360_tui.py --selftest  # mock-based selftests, no TTY required

python y360_gui.py --mock       # GUI with demo data (Windows: the python.org build already includes Tcl/Tk)
```

TUI controls: arrow keys / `j` `k`, digits 1–9 for quick selection,
Enter to open, Backspace or `x` to go back, `q` to quit.

Requires Python 3.8+. No third-party dependencies.
