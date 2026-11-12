"""Airtime demo endpoints (attached to the tool service on the Lagos box).

Purpose: let a demo agent sell airtime on a call. The purchase is simulated -
no carrier is contacted - but everything around it is real: the call goes
through the voice worker, the purchase is journalled, and a notification email
is sent through the platform notification service so the caller can see the
alert land in their inbox.

Routes (all require the service token, like every other tool route):
  GET  /v1/demo/airtime/balance
  POST /v1/demo/airtime/purchase   {network, phone_number, amount, request_id?}

The route is served from inside the existing tool container so the voice
worker reaches it over the internal socket (no public exposure, no new
container).

Environment (all optional):
  DEMO_AIRTIME_ALERT_EMAIL   default recipient of the purchase email
  DEMO_AIRTIME_START_BALANCE mock starting balance in naira (default 25000)
  DEMO_AIRTIME_JOURNAL       journal path (default /sandbox-airtime/demo-airtime.jsonl)
  DEMO_AIRTIME_ALERT_URL     notification endpoint (default http://127.0.0.1:8093/v1/messages/email/send)
  DEMO_AIRTIME_ALERT_TOKEN   notification service token (falls back to the tool service token)
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request

NETWORKS = ("MTN", "Airtel", "Glo", "9mobile")
MIN_AMOUNT = 50
MAX_AMOUNT = 50_000

_LOCK = threading.Lock()


def _journal_path() -> Path:
    return Path(os.getenv("DEMO_AIRTIME_JOURNAL", "/sandbox-airtime/demo-airtime.jsonl"))


def _start_balance() -> int:
    try:
        return int(os.getenv("DEMO_AIRTIME_START_BALANCE", "25000"))
    except ValueError:
        return 25_000


def _read_journal() -> list[dict]:
    path = _journal_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _append_journal(row: dict) -> None:
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _wallet_state(rows: list[dict], business_id: str) -> tuple[int, str]:
    """Balance and last reference for one business (a fresh demo wallet each time
    the journal is cleared)."""
    balance = _start_balance()
    reference = ""
    for row in rows:
        if row.get("business_id") != business_id or row.get("kind") != "purchase":
            continue
        reference = row.get("reference") or reference
        if row.get("balance_after") is not None:
            balance = int(row["balance_after"])
    return balance, reference


def _normalise_network(value: object) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "mtn": "MTN",
        "airtel": "Airtel",
        "zain": "Airtel",
        "glo": "Glo",
        "globacom": "Glo",
        "9mobile": "9mobile",
        "9 mobile": "9mobile",
        "etisalat": "9mobile",
    }
    if text not in aliases:
        raise HTTPException(status_code=400, detail="Unknown network for airtime.")
    return aliases[text]


def _normalise_phone(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 13 and digits.startswith("234"):
        digits = "0" + digits[3:]
    if len(digits) != 11 or not digits.startswith("0") or digits[1] < "7":
        raise HTTPException(status_code=400, detail="A valid Nigerian phone number is required.")
    return digits


def _normalise_amount(value) -> int:
    try:
        amount = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid airtime amount is required.")
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Airtime must be between {MIN_AMOUNT} and {MAX_AMOUNT} naira.",
        )
    return amount


def _config(name: str, default: str = "") -> str:
    """A setting from the environment, else from the local config file.

    The container environment is fixed at create time, so the demo alert
    recipient/token live in a 0600 JSON file inside the bind-mounted state
    directory instead of forcing a compose recreate.
    """
    value = str(os.getenv(name, "")).strip()
    if value:
        return value
    path = Path(os.getenv("DEMO_AIRTIME_CONFIG", "/sandbox-airtime/demo-airtime.json"))
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get(name, "") or default).strip()
    except (OSError, json.JSONDecodeError):
        pass
    return default


def _send_alert(*, network: str, phone_number: str, amount: int, reference: str, balance: int) -> tuple[bool, str]:
    recipient = _config("DEMO_AIRTIME_ALERT_EMAIL")
    if not recipient:
        return False, "no recipient configured"
    url = str(
        os.getenv("DEMO_AIRTIME_ALERT_URL", "http://127.0.0.1:8093/v1/messages/email/send")
    ).strip()
    token = _config(
        "DEMO_AIRTIME_ALERT_TOKEN", os.getenv("WEMA_TOOLS_SERVICE_TOKEN", "")
    )
    now = datetime.now(timezone.utc).astimezone()
    payload = {
        "email": recipient,
        "subject": f"Airtime purchase of N{amount:,} on {network}",
        "title": "Airtime purchase successful",
        "intro": (
            f"A debit of N{amount:,} was made from your wallet for {network} airtime "
            f"on {phone_number}."
        ),
        "highlight_label": "Reference",
        "highlight_value": reference,
        "outro": (
            f"Transaction time: {now.strftime('%d %b %Y, %I:%M %p')}. "
            f"Available balance: N{balance:,}. If you did not authorise this purchase, "
            "please contact support immediately."
        ),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Service-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if 200 <= response.status < 300:
                return True, ""
            return False, f"notification HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"notification HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - the purchase must not fail on email trouble
        return False, f"notification unavailable: {type(exc).__name__}"


def register_demo_airtime(app, authorize) -> None:
    """Attach the demo airtime routes to an existing app."""

    def _business(request: Request) -> str:
        return request.headers.get("X-Business-Id", "").strip()

    @app.get("/v1/demo/airtime/balance", dependencies=[Depends(authorize)])
    def balance(request: Request):
        with _LOCK:
            rows = _read_journal()
        value, reference = _wallet_state(rows, _business(request))
        return {
            "status": "success",
            "message": f"Your airtime balance is {value} naira.",
            "data": {"balance": value, "currency": "NGN", "last_reference": reference},
        }

    @app.post("/v1/demo/airtime/purchase", dependencies=[Depends(authorize)])
    async def purchase(request: Request):
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        network = _normalise_network(body.get("network"))
        phone_number = _normalise_phone(body.get("phone_number"))
        amount = _normalise_amount(body.get("amount"))
        request_id = str(body.get("request_id") or "").strip()
        business_id = _business(request)
        agent_id = request.headers.get("X-Agent-Id", "").strip()
        caller = str(body.get("caller") or request.headers.get("X-End-User-Id") or "").strip()

        with _LOCK:
            rows = _read_journal()
            if request_id:
                for row in rows:
                    if row.get("kind") == "purchase" and row.get("request_id") == request_id:
                        return {
                            "status": "success",
                            "message": row.get("message") or "That purchase already went through.",
                            "data": row.get("data") or {},
                        }
            value, _ = _wallet_state(rows, business_id)
            if amount > value:
                return {
                    "status": "failed",
                    "message": (
                        f"There isn't enough airtime balance for {amount} naira. "
                        f"Your balance is {value} naira."
                    ),
                    "data": {"balance": value, "currency": "NGN"},
                }
            balance_after = value - amount
            reference = f"AIR-{secrets.token_hex(3).upper()}"
            message = (
                f"{amount} naira of {network} airtime is now on {phone_number}. "
                f"Reference {reference}. Your balance is {balance_after} naira."
            )
            data = {
                "reference": reference,
                "network": network,
                "phone_number": phone_number,
                "amount": amount,
                "currency": "NGN",
                "balance": balance_after,
            }
            _append_journal(
                {
                    "kind": "purchase",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "business_id": business_id,
                    "agent_id": agent_id,
                    "caller": caller,
                    "request_id": request_id,
                    "network": network,
                    "phone_number": phone_number,
                    "amount": amount,
                    "reference": reference,
                    "balance_after": balance_after,
                    "message": message,
                    "data": data,
                }
            )

        email_sent, email_error = _send_alert(
            network=network,
            phone_number=phone_number,
            amount=amount,
            reference=reference,
            balance=balance_after,
        )
        data["email_sent"] = email_sent
        if email_error:
            data["email_error"] = email_error
        return {"status": "success", "message": message, "data": data}
