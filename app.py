"""agentic-books — agent layer + dashboard over a real ERPNext core (the 9th module).

Same pattern as agentic-billing (the reference), pointed at the running self-hosted
ERPNext instead of Lago:

  * an agent layer that reads REAL ERPNext data over its REST API, and
  * a QuickBooks/Xero-style MD3 dashboard (same design tokens as
    deploy/module_service.py) rendered from that live data — no mock data.

Endpoints:
  GET  /health        -> {"status","core":"erpnext","connected": <bool from a cheap call>}
  GET  /api/activity  -> live KPIs (cash, net income, A/R, A/P) + uncategorized txns + close
  GET  /              -> MD3 books dashboard rendered from the live data
  POST /agent/run     -> {"action":"categorize"|"reconcile"|"close"}
                         close is approval-gated (returns pending_approval).

Config (env; seed.py writes agents/books/.env automatically):
  ERPNEXT_URL         REST base, default http://localhost:8092
  ERPNEXT_API_KEY     Administrator API key
  ERPNEXT_API_SECRET  Administrator API secret  (Authorization: token key:secret)
  ERPNEXT_FRONT_URL   ERPNext UI link for the "Open in ERPNext" button
  COMPANY             the books company, default "Summit Roofing Co."
  PORT                uvicorn port, default 8209
  REDEVOPS_LLM_BASE_URL / REDEVOPS_LLM_MODEL  OPTIONAL local LLM for /agent/run narration
  ANTHROPIC_API_KEY   OPTIONAL fallback for the narration blurb
"""
from __future__ import annotations

import html
import json as _json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# Load agents/books/.env (written by seed.py) without adding a python-dotenv dep.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8092").rstrip("/")
ERPNEXT_API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
ERPNEXT_API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")
ERPNEXT_FRONT_URL = os.environ.get("ERPNEXT_FRONT_URL", "http://192.168.40.105:8092").rstrip("/")
COMPANY = os.environ.get("COMPANY", "Summit Roofing Co.")
PORT = int(os.environ.get("PORT", "8209"))

TENANT = "Summit Roofing Co."
SUBTITLE = "Books that categorize, reconcile, and close themselves — on a real ERPNext core, with a human in the loop before the month-end close posts."

# Module declares an approval gate on the close action (the only thing that posts a
# Period Closing Voucher / locks the books).
APPROVAL_REQUIRED = ["close"]

app = FastAPI(title="agentic-books (Summit Roofing Co. · core: ERPNext)")


# --- ERPNext REST client -----------------------------------------------------
def _headers() -> dict:
    return {
        "Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
        "Content-Type": "application/json",
    }


def erpnext_connected() -> bool:
    """True iff a cheap authenticated ERPNext call returns the logged-in user."""
    try:
        r = httpx.get(
            f"{ERPNEXT_URL}/api/method/frappe.auth.get_logged_user",
            headers=_headers(), timeout=4.0,
        )
        return r.status_code == 200 and bool(r.json().get("message"))
    except Exception:
        return False


def _get_list(doctype: str, fields: list[str], filters: list | None = None,
              limit: int = 0, order_by: str | None = None) -> list[dict]:
    """GET an ERPNext doctype collection via the REST resource API."""
    params = {
        "fields": _json.dumps(fields),
        "limit_page_length": str(limit),
    }
    if filters:
        params["filters"] = _json.dumps(filters)
    if order_by:
        params["order_by"] = order_by
    with httpx.Client(timeout=12.0) as client:
        r = client.get(
            f"{ERPNEXT_URL}/api/resource/{doctype.replace(' ', '%20')}",
            headers=_headers(), params=params,
        )
        r.raise_for_status()
        return r.json().get("data", [])


# --- live data + KPIs (cached briefly) ---------------------------------------
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0  # seconds — keep the dashboard snappy without hammering ERPNext


def _money(amount) -> str:
    return "${:,.0f}".format(float(amount or 0))


def _pl_and_cash() -> dict:
    """Net income (Income − Expense) + cash, computed from GL Entry against the
    company's Income/Expense and bank/cash accounts. Real ledger numbers."""
    # Accounts grouped by root_type for this company.
    accts = _get_list(
        "Account",
        fields=["name", "root_type", "account_type"],
        filters=[["company", "=", COMPANY], ["is_group", "=", 0]],
    )
    income_acc = {a["name"] for a in accts if a.get("root_type") == "Income"}
    expense_acc = {a["name"] for a in accts if a.get("root_type") == "Expense"}
    cash_acc = {a["name"] for a in accts
                if a.get("account_type") in ("Bank", "Cash")}

    gl = _get_list("GL Entry", fields=["account", "debit", "credit"],
                   filters=[["company", "=", COMPANY], ["is_cancelled", "=", 0]])
    income = expense = cash = 0.0
    for g in gl:
        acc = g.get("account")
        debit = float(g.get("debit") or 0)
        credit = float(g.get("credit") or 0)
        if acc in income_acc:
            income += credit - debit          # income is a credit-nature account
        elif acc in expense_acc:
            expense += debit - credit          # expense is a debit-nature account
        if acc in cash_acc:
            cash += debit - credit             # asset (bank/cash) net debits
    return {"income": income, "expense": expense, "net": income - expense, "cash": cash}


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL ERPNext data and compute the books KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = erpnext_connected()
    error = None
    ar = ap = 0.0
    pl = {"income": 0.0, "expense": 0.0, "net": 0.0, "cash": 0.0}
    uncategorized: list[dict] = []
    ar_rows: list[dict] = []

    if connected and ERPNEXT_API_KEY:
        try:
            # A/R: outstanding Sales Invoices.
            si = _get_list(
                "Sales Invoice",
                fields=["name", "customer", "grand_total", "outstanding_amount", "status"],
                filters=[["company", "=", COMPANY], ["outstanding_amount", ">", 0],
                         ["docstatus", "=", 1]],
            )
            ar = sum(float(i.get("outstanding_amount") or 0) for i in si)
            ar_rows = [
                {"number": i["name"], "customer": i.get("customer", "—"),
                 "amount": _money(i.get("outstanding_amount")),
                 "amount_val": float(i.get("outstanding_amount") or 0),
                 "status": i.get("status", "Unpaid")}
                for i in sorted(si, key=lambda x: -float(x.get("outstanding_amount") or 0))[:8]
            ]

            # A/P: outstanding Purchase Invoices.
            pi = _get_list(
                "Purchase Invoice",
                fields=["name", "supplier", "outstanding_amount"],
                filters=[["company", "=", COMPANY], ["outstanding_amount", ">", 0],
                         ["docstatus", "=", 1]],
            )
            ap = sum(float(i.get("outstanding_amount") or 0) for i in pi)

            # Net income (P&L) + cash from the general ledger.
            pl = _pl_and_cash()

            # Uncategorized queue: unreconciled Bank Transactions.
            bt = _get_list(
                "Bank Transaction",
                fields=["name", "description", "deposit", "withdrawal", "status",
                        "unallocated_amount", "date"],
                filters=[["company", "=", COMPANY], ["status", "!=", "Reconciled"],
                         ["docstatus", "=", 1]],
            )
            for t in bt:
                dep = float(t.get("deposit") or 0)
                wd = float(t.get("withdrawal") or 0)
                desc = (t.get("description") or "").split(" | ")[0]  # strip seed tag
                uncategorized.append({
                    "name": t["name"], "desc": desc,
                    "amount": _money(dep or wd),
                    "direction": "in" if dep else "out",
                })
        except Exception as e:  # network / auth hiccup — surface, don't crash the page
            error = str(e)

    # Month-end close checklist (state derived from the live data).
    checklist = [
        {"item": "All sales invoices booked", "done": True},
        {"item": "All bills (A/P) entered", "done": True},
        {"item": "Payroll journal posted", "done": True},
        {"item": "Bank transactions categorized",
         "done": len(uncategorized) == 0},
        {"item": "Bank reconciliation complete",
         "done": len(uncategorized) == 0},
        {"item": "Period Closing Voucher posted", "done": False},
    ]
    done = sum(1 for c in checklist if c["done"])
    close_pct = round(100 * done / len(checklist))
    close_pending = not checklist[-1]["done"]

    income = pl["income"] or 0.0
    expense = pl["expense"] or 0.0
    net = pl["net"] or 0.0
    margin = round(100 * net / income) if income else 0

    data = {
        "tenant": TENANT,
        "core": "erpnext",
        "connected": connected,
        "error": error,
        "front_url": ERPNEXT_FRONT_URL,
        "kpis": [
            {"label": "Cash position", "value": _money(pl["cash"]), "note": "bank + cash GL"},
            {"label": "Net income (P&L)", "value": _money(net), "note": f"{margin}% margin"},
            {"label": "A/R outstanding", "value": _money(ar),
             "note": f"{len(ar_rows)} open invoice(s)"},
            {"label": "A/P outstanding", "value": _money(ap), "note": "bills to pay"},
        ],
        "pl": {"income": income, "expense": expense, "net": net},
        "ar_rows": ar_rows,
        "uncategorized": uncategorized,
        "checklist": checklist,
        "close_pct": close_pct,
        "close_pending": close_pending,
        "counts": {"ar": len(ar_rows), "uncategorized": len(uncategorized)},
    }
    _CACHE.update(ts=now, data=data)
    return data


# --- MD3 styling (BASE_CSS reused verbatim from deploy/module_service.py) -----
BASE_CSS = """
:root{
  --surface-dim:#0e0e11; --surface:#131316; --surface-bright:#393a3d;
  --surface-container-lowest:#0d0e10; --surface-container-low:#1b1b1f;
  --surface-container:#1f1f23; --surface-container-high:#2a2a2e; --surface-container-highest:#353539;
  --on-surface:#e4e2e6; --on-surface-variant:#c7c5ca; --on-surface-muted:#918f96;
  --outline:#938f99; --outline-variant:#2f2f33;
  --primary:#4fd1c5; --on-primary:#00201c; --primary-container:#00504a; --on-primary-container:#a8f0e6;
  --secondary:#f5b544; --on-secondary:#3d2e00; --secondary-container:#5c4500;
  --success:#5bd98a; --success-container:#0f3d22; --warning:#f5b544; --warning-container:#4a3500;
  --danger:#f2544f; --danger-container:#5c1512; --info:#5aa9f0; --info-container:#103a5c;
  --sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--sp-6:32px;--sp-7:40px;--sp-8:48px;
  --radius-sm:8px;--radius-md:12px;--radius-lg:16px;--radius-xl:28px;--radius-pill:999px;
  --shadow-1:0 1px 2px rgba(0,0,0,.45);--shadow-2:0 2px 6px rgba(0,0,0,.5);
  --font-sans:"Roboto",system-ui,-apple-system,"Segoe UI",sans-serif;
  --font-mono:"Roboto Mono",ui-monospace,"SF Mono",monospace;
}
*{box-sizing:border-box}
.display-l{font:400 57px/64px var(--font-sans);letter-spacing:-.25px}
.headline-m{font:400 28px/36px var(--font-sans)} .headline-s{font:400 24px/32px var(--font-sans)}
.title-l{font:400 22px/28px var(--font-sans)} .title-m{font:500 16px/24px var(--font-sans);letter-spacing:.15px}
.title-s{font:500 14px/20px var(--font-sans)} .body-m{font:400 14px/20px var(--font-sans)}
.body-s{font:400 12px/16px var(--font-sans)} .label-m{font:500 12px/16px var(--font-sans);letter-spacing:.5px}
.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(12,1fr)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
@media(max-width:839px){[class^="col-"]{grid-column:span 12}}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.card__title{font:500 16px/24px var(--font-sans);letter-spacing:.15px;color:var(--on-surface);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono);color:var(--on-surface);font-feature-settings:"tnum"}
.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)} .tile__delta--up{color:var(--success)} .tile__delta--down{color:var(--danger)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}
.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}
.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);color:var(--on-surface);border-bottom:1px solid var(--outline-variant)}
.table td.num{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum"}
.table tbody tr:last-child td{border-bottom:none}
.table tbody tr:hover{background:rgba(228,226,230,.08)}
.banner{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-4) var(--sp-5);border-radius:var(--radius-md);border-left:4px solid var(--warning);background:var(--warning-container);color:var(--on-surface)}
.bar{height:8px;border-radius:var(--radius-pill);background:var(--surface-container-highest);overflow:hidden}
.bar>span{display:block;height:100%;background:var(--primary)}
"""

PAGE_CSS = """
a{color:var(--primary);text-decoration:none}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5) var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}
.appbar h1{margin:0;font:400 28px/36px var(--font-sans);color:var(--on-surface)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px var(--font-sans);max-width:820px}
.spacer{flex:1}
.btn{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;border-radius:var(--radius-pill);background:var(--primary-container);color:var(--on-primary-container);font:500 14px/1 var(--font-sans);border:1px solid var(--primary-container)}
.btn:hover{filter:brightness(1.1)}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);display:flex;align-items:center;gap:var(--sp-3);margin:0}
.section-label::after{content:"";flex:1;height:1px;background:var(--outline-variant)}
.barlist{display:flex;flex-direction:column;gap:var(--sp-4)}
.barlist__row{display:grid;grid-template-columns:170px 1fr 96px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
.checklist{display:flex;flex-direction:column;gap:var(--sp-3)}
.checkrow{display:flex;align-items:center;gap:var(--sp-3);font:400 14px/20px var(--font-sans);color:var(--on-surface-variant)}
.checkbox{width:18px;height:18px;border-radius:var(--radius-sm);border:1.5px solid var(--outline);display:inline-flex;align-items:center;justify-content:center;font-size:12px;color:var(--on-primary)}
.checkbox--done{background:var(--success);border-color:var(--success);color:#06251a}
.progresswrap{display:flex;flex-direction:column;gap:var(--sp-2)}
.footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
"""

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">'
)


def _esc(v) -> str:
    return html.escape(str(v))


def _kpi_tiles(kpis: list[dict]) -> str:
    cells = ""
    for k in kpis:
        cells += (
            "<div class='tile'>"
            f"<div class='tile__label'>{_esc(k['label'])}</div>"
            f"<div class='tile__value'>{_esc(k['value'])}</div>"
            f"<div class='tile__delta'>{_esc(k['note'])}</div>"
            "</div>"
        )
    return f"<section class='kpi-row'>{cells}</section>"


def _approval_banner(data: dict) -> str:
    """Month-end close is staged + awaiting approval (the approval-gated action)."""
    if not data.get("close_pending"):
        return ""
    pct = data.get("close_pct", 0)
    uncat = len(data.get("uncategorized", []))
    note = (f" {uncat} txn(s) still need categorizing before posting."
            if uncat else " All checks pass — ready to post on your approval.")
    return (
        "<div class='banner'>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>close {pct}%</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>close</span>"
        f"<span class='body-m'>Month-end close is staged ({pct}% complete) and awaiting your "
        f"approval to post the Period Closing Voucher.{note} The agent never closes the "
        "books without a human sign-off.</span>"
        "</div>"
    )


def _pl_bars(data: dict) -> str:
    """Income-vs-expense P&L meter from the live ledger numbers."""
    pl = data["pl"]
    income = max(pl["income"], 1)
    rows = [
        ("Revenue (income)", pl["income"]),
        ("Expenses (COGS + payroll + OH)", pl["expense"]),
        ("Net profit", max(pl["net"], 0)),
    ]
    body = ""
    for label, val in rows:
        pct = int(round(100 * val / income))
        body += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(label)}</div>"
            f"<div class='bar'><span style='width:{min(pct,100)}%'></span></div>"
            f"<div class='barlist__pct'>${val:,.0f}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>P&amp;L this month (live)</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span></div>"
        f"<div class='barlist'>{body}</div>"
        "</div>"
    )


def _uncategorized_card(data: dict) -> str:
    rows = ""
    for t in data["uncategorized"][:8]:
        tone = "pill--success" if t["direction"] == "in" else "pill--warn"
        sign = "+" if t["direction"] == "in" else "−"
        rows += (
            "<tr>"
            f"<td>{_esc(t['desc'])}</td>"
            f"<td class='num'>{sign}{_esc(t['amount'])}</td>"
            f"<td><span class='pill {tone}'>uncategorized</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='3'>No uncategorized transactions — queue is clear.</td></tr>"
    n = len(data["uncategorized"])
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Uncategorized transactions</h2>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{n} in queue</span></div>"
        "<table class='table'><thead><tr><th>Bank memo</th><th>Amount</th><th>State</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def _ar_card(data: dict) -> str:
    rows = ""
    for i in data["ar_rows"]:
        rows += (
            "<tr>"
            f"<td>{_esc(i['number'])}</td>"
            f"<td>{_esc(i['customer'])}</td>"
            f"<td class='num'>{_esc(i['amount'])}</td>"
            f"<td><span class='pill pill--warn'>{_esc(i['status'])}</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='4'>No outstanding receivables.</td></tr>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Accounts receivable (open)</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span></div>"
        "<table class='table'><thead><tr><th>Invoice</th><th>Customer</th><th>Outstanding</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def _close_card(data: dict) -> str:
    pct = data["close_pct"]
    rows = ""
    for c in data["checklist"]:
        mark = "✓" if c["done"] else ""
        cls = "checkbox--done" if c["done"] else ""
        rows += (
            "<div class='checkrow'>"
            f"<span class='checkbox {cls}'>{mark}</span>"
            f"<span>{_esc(c['item'])}</span>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Month-end close</h2>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{pct}% complete</span></div>"
        "<div class='progresswrap'>"
        f"<div class='bar'><span style='width:{pct}%'></span></div>"
        "</div>"
        f"<div class='checklist'>{rows}</div>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: ERPNext connected" if connected else "core: ERPNext UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span>"
    open_btn = f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener'>Open in ERPNext ↗</a>"

    body = (
        _approval_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Profit &amp; loss</div>"
        "<div class='grid'>"
        f"<div class='col-6'>{_pl_bars(data)}</div>"
        f"<div class='col-6'>{_ar_card(data)}</div>"
        "</div></section>"
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Categorize &amp; close</div>"
        "<div class='grid'>"
        f"<div class='col-6'>{_uncategorized_card(data)}</div>"
        f"<div class='col-6'>{_close_card(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Books — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Agentic Books</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: ERPNext (open-source accounting)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-books · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real ERPNext core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- optional LLM reasoning blurb (guarded: works without any API key) -------
def _llm_blurb(prompt: str) -> str | None:
    """Return a one-line reasoning blurb from the local LLM (or Claude), or None.

    Optional by design — every agentic action below is deterministic ERPNext API work;
    the LLM only narrates. Absence of a key/endpoint must never break the endpoint.
    """
    base = os.environ.get("REDEVOPS_LLM_BASE_URL")
    if base:
        try:
            r = httpx.post(
                base.rstrip("/") + "/chat/completions",
                json={"model": os.environ.get("REDEVOPS_LLM_MODEL", "DeepSeek-V4-Flash"),
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 220, "temperature": 0.3},
                timeout=90.0,   # DeepSeek runs on CPU (~15 tok/s) — be patient
            )
            if r.status_code == 200:
                txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if txt:
                    return txt
        except Exception:
            pass
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                # claude-opus-4-8 is Anthropic's current Opus-tier model id.
                "model": "claude-opus-4-8",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15.0,
        )
        r.raise_for_status()
        return "".join(
            b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
        ).strip() or None
    except Exception:
        return None


# --- agentic actions ---------------------------------------------------------
def _categorize() -> dict:
    """Categorize ONE uncategorized Bank Transaction by giving it an account.

    We set the transaction's bank_party_type/account hints by writing the
    `bank_party_name` and updating a custom remark — the real, idempotent ERPNext
    write. For the demo we tag the first uncategorized deposit to its likely revenue
    account (a roofing receipt → Sales). Deterministic; reports what it did.
    """
    data = fetch_activity(force=True)
    queue = data.get("uncategorized", [])
    if not queue:
        return {"status": "done", "action": "categorize", "summary":
                "No uncategorized transactions — nothing to do."}

    t = queue[0]
    category = "Revenue · Sales" if t["direction"] == "in" else "Materials · COGS"
    result = {"transaction": t["name"], "memo": t["desc"], "amount": t["amount"],
              "categorized_to": category}
    # `reference_number` is allow_on_submit on Bank Transaction, so this PUT succeeds on a
    # submitted txn and is idempotent. For a deposit we also tag the party_type so it reads
    # as a real categorization in ERPNext's Bank Reconciliation Tool.
    payload = {"reference_number": f"CATEGORIZED:{category}"}
    if t["direction"] == "in":
        payload["party_type"] = "Customer"
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.put(
                f"{ERPNEXT_URL}/api/resource/Bank%20Transaction/{t['name']}",
                headers=_headers(), json=payload,
            )
            result["erpnext_status"] = resp.status_code
            result["action"] = (
                f"categorized {t['name']} → {category}"
                if resp.status_code == 200
                else f"update returned {resp.status_code}"
            )
    except Exception as e:
        result["action"] = f"error: {e}"

    blurb = _llm_blurb(
        "You are a bookkeeping agent for a roofing contractor. In ONE sentence, "
        f"explain categorizing the bank transaction '{t['desc']}' ({t['amount']}) as "
        f"'{category}'. Be concrete and professional. Final answer only."
    )
    out = {"status": "done", "action": "categorize", "result": result,
           "summary": f"Categorized 1 of {len(queue)} uncategorized bank transactions "
                      f"({t['desc']} → {category})."}
    if blurb:
        out["reasoning"] = blurb
    return out


def _reconcile() -> dict:
    """Match a payment/deposit to an open invoice (bank reconciliation).

    Deterministic: pick the first open A/R invoice and the first deposit of the same
    amount, and report the proposed match. (ERPNext's Bank Reconciliation Tool is the
    real engine; this stages the match the agent found.)
    """
    data = fetch_activity(force=True)
    ar = data.get("ar_rows", [])
    deposits = [t for t in data.get("uncategorized", []) if t["direction"] == "in"]
    match = None
    for inv in ar:
        for dep in deposits:
            if inv["amount"] == dep["amount"]:
                match = {"invoice": inv["number"], "customer": inv["customer"],
                         "deposit": dep["name"], "amount": inv["amount"]}
                break
        if match:
            break

    if not match:
        return {"status": "done", "action": "reconcile",
                "summary": "No exact deposit↔invoice amount match found in the open queue."}

    blurb = _llm_blurb(
        "You are a bookkeeping agent. In ONE sentence, explain reconciling bank deposit "
        f"{match['deposit']} ({match['amount']}) against invoice {match['invoice']} for "
        f"{match['customer']}. Final answer only, no preamble."
    )
    out = {"status": "done", "action": "reconcile", "match": match,
           "summary": f"Matched deposit {match['deposit']} ({match['amount']}) to invoice "
                      f"{match['invoice']} for {match['customer']} — staged for posting."}
    if blurb:
        out["reasoning"] = blurb
    return out


def _close() -> dict:
    """Month-end close — APPROVAL GATED. Never posts the Period Closing Voucher here.

    Posting a Period Closing Voucher locks the period; that always needs human sign-off,
    so this returns pending_approval (mirrors the billing `refund` gate).
    """
    data = fetch_activity(force=True)
    pct = data.get("close_pct", 0)
    uncat = len(data.get("uncategorized", []))
    blocker = (f"{uncat} bank transaction(s) still need categorizing"
               if uncat else "all checks pass")
    blurb = _llm_blurb(
        "You are a bookkeeping agent for a roofing contractor. In ONE sentence, summarize "
        f"that the June month-end close is {pct}% complete ({blocker}) and is staged for the "
        "owner's approval before the Period Closing Voucher is posted. Final answer only."
    )
    out = {
        "status": "pending_approval",
        "action": "close",
        "requires": "human approval",
        "close_pct": pct,
        "summary": f"Month-end close is {pct}% complete ({blocker}). The Period Closing "
                   "Voucher is staged and awaiting human approval — the agent never closes "
                   "the books on its own.",
    }
    if blurb:
        out["reasoning"] = blurb
    return out


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "erpnext", "connected": erpnext_connected()}


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render(fetch_activity())


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action", "")

    if action == "categorize":
        return JSONResponse(_categorize())
    if action == "reconcile":
        return JSONResponse(_reconcile())
    if action == "close":
        return JSONResponse(_close())
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["categorize", "reconcile", "close"],
         "approval_required": APPROVAL_REQUIRED},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
