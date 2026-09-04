import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
ZEPTOMAIL_API_URL = "https://api.zeptomail.com/v1.1/email"


def zepto_mail_configured() -> bool:
    return bool(str(os.getenv("ZEPTOMAIL_SMTP_PASSWORD") or "").strip()) and bool(
        str(os.getenv("ZEPTOMAIL_FROM_EMAIL") or "").strip()
    )


def _smtp_settings() -> dict[str, Any]:
    return {
        "host": str(os.getenv("ZEPTOMAIL_SMTP_HOST") or "smtp.zeptomail.com").strip(),
        "port": int(str(os.getenv("ZEPTOMAIL_SMTP_PORT") or "587").strip() or "587"),
        "username": str(os.getenv("ZEPTOMAIL_SMTP_USERNAME") or "emailapikey").strip(),
        "password": str(os.getenv("ZEPTOMAIL_SMTP_PASSWORD") or "").strip(),
        "from_email": str(os.getenv("ZEPTOMAIL_FROM_EMAIL") or "").strip(),
        "from_name": str(os.getenv("ZEPTOMAIL_FROM_NAME") or "Wema Auth Demo").strip(),
    }


def _send_smtp_email(*, to_email: str, subject: str, body_text: str) -> None:
    settings = _smtp_settings()
    if not settings["password"] or not settings["from_email"]:
        raise RuntimeError("ZeptoMail is not fully configured.")

    msg = EmailMessage()
    msg["Subject"] = subject
    from_header = settings["from_email"]
    if settings["from_name"]:
        from_header = f"{settings['from_name']} <{settings['from_email']}>"
    msg["From"] = from_header
    msg["To"] = to_email
    msg.set_content(body_text)

    port = int(settings["port"])
    if port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings["host"], port, context=context) as server:
            server.login(settings["username"], settings["password"])
            server.send_message(msg)
        return

    with smtplib.SMTP(settings["host"], port, timeout=20) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(settings["username"], settings["password"])
        server.send_message(msg)


def _send_zepto_api_email(*, to_email: str, subject: str, body_text: str) -> None:
    settings = _smtp_settings()
    api_key = settings["password"]
    authorization = (
        api_key
        if api_key.lower().startswith("zoho-enczapikey")
        else f"Zoho-enczapikey {api_key}"
    )
    payload = {
        "from": {"address": settings["from_email"], "name": settings["from_name"]},
        "to": [{"email_address": {"address": to_email, "name": to_email}}],
        "subject": subject,
        "textbody": body_text,
        "htmlbody": f"<p>{body_text}</p>",
    }
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            ZEPTOMAIL_API_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": authorization,
            },
            json=payload,
        )
    if response.status_code >= 400:
        detail = response.text[:300]
        lowered = detail.lower()
        if "le_102" in lowered or "credit" in lowered or "limit exhausted" in lowered:
            raise RuntimeError("ZeptoMail sending credits are exhausted.")
        raise RuntimeError(
            f"ZeptoMail API rejected email status={response.status_code} body={detail}"
        )


async def send_zepto_email(
    *,
    to_email: str,
    subject: str,
    body_text: str,
) -> dict[str, Any]:
    recipient = str(to_email or "").strip()
    title = str(subject or "").strip() or "Voice agent message"
    body = str(body_text or "").strip()
    if not recipient or "@" not in recipient:
        return {
            "status": "failed",
            "sent": False,
            "message": "A valid recipient email is required.",
            "to": recipient,
            "subject": title,
        }
    if not zepto_mail_configured():
        return {
            "status": "failed",
            "sent": False,
            "mocked": True,
            "message": "ZeptoMail is not configured on this voice worker.",
            "to": recipient,
            "subject": title,
        }

    last_error = ""
    for sender, provider in (
        (_send_zepto_api_email, "zeptomail-api"),
        (_send_smtp_email, "zeptomail-smtp"),
    ):
        try:
            await asyncio.to_thread(
                sender,
                to_email=recipient,
                subject=title,
                body_text=body,
            )
            logger.info("ZeptoMail send succeeded via %s to=%s subject=%s", provider, recipient, title)
            return {
                "status": "success",
                "sent": True,
                "mocked": False,
                "provider": provider,
                "to": recipient,
                "subject": title,
                "message": "Email sent.",
            }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("ZeptoMail %s send failed to=%s: %s", provider, recipient, exc)
            if "credits are exhausted" in last_error.lower():
                break

    return {
        "status": "failed",
        "sent": False,
        "message": f"Email delivery failed: {last_error}",
        "to": recipient,
        "subject": title,
    }
