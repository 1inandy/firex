"""Area alerts: notify when an area's latest month reaches N× its historical median.

Subscriptions live in data/alerts.json (gitignored). Checking is explicit: `POST /alerts/check`, or run
`python -m backend.app.alerts` from cron after each data refresh. Each alert fires at most once per
calendar month.

Delivery (all optional, configured in .env):
  * email:   SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, ALERT_FROM. Without SMTP_HOST the
             notification is only logged.
  * webhook: an https URL per alert; receives a JSON POST (works with Slack incoming webhooks and most
             SMS gateways).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import smtplib
import threading
import urllib.request
import uuid
from email.message import EmailMessage

from pipeline import config

from . import area as area_mod
from . import queries as q

log = logging.getLogger("firex.alerts")
_lock = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _load() -> list[dict]:
    p = config.ALERTS_FILE
    return json.loads(p.read_text()) if p.exists() else []


def _save(alerts: list[dict]) -> None:
    p = config.ALERTS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(alerts, indent=2))
    tmp.replace(p)


def validate_target(email: str | None, webhook: str | None) -> None:
    if email and not EMAIL_RE.match(email):
        raise ValueError("email doesn't look valid")
    if webhook and not webhook.startswith("https://"):
        raise ValueError("webhook must be an https:// URL")


def create(params: dict[str, str], threshold: float, name: str | None = None,
           email: str | None = None, webhook: str | None = None) -> dict:
    a = area_mod.parse(**params)  # validates
    if not 1.0 < threshold <= 20:
        raise ValueError("threshold must be between 1 and 20 (× median)")
    validate_target(email, webhook)
    alert = {"id": uuid.uuid4().hex[:10], "name": (name or "").strip()[:80] or None,
             "area": area_mod.to_params(a), "threshold": float(threshold),
             "email": email or None, "webhook": webhook or None,
             "created": dt.datetime.now().isoformat(timespec="seconds"), "last_notified": None}
    with _lock:
        alerts = _load()
        alerts.append(alert)
        _save(alerts)
    return alert


def delete(alert_id: str) -> bool:
    with _lock:
        alerts = _load()
        keep = [a for a in alerts if a["id"] != alert_id]
        if len(keep) == len(alerts):
            return False
        _save(keep)
    return True


def public(alert: dict) -> dict:
    """Alert as returned by the API (contact details masked)."""
    out = {k: v for k, v in alert.items() if k not in ("email", "webhook")}
    if alert.get("email"):
        user, _, dom = alert["email"].partition("@")
        out["email"] = f"{user[:1]}…@{dom}"
    out["webhook"] = bool(alert.get("webhook"))
    return out


def status(alert: dict) -> dict:
    """Evaluate one alert against the latest month (no notification)."""
    cur = q.current(area_mod.parse(**alert["area"]))
    value, median, ratio = cur["value"], cur["median"], cur["ratio"]
    enough = value >= config.ALERT_MIN_CELL_DAYS
    triggered = bool(enough and (ratio >= alert["threshold"] if ratio is not None else value > 0))
    return {"triggered": triggered, "year": cur["year"], "month": cur["month"], "value": value,
            "median": median, "ratio": ratio, "partial": cur["partial"], "source": cur["source"]}


def _message(alert: dict, st: dict) -> tuple[str, str]:
    label = alert.get("name") or ", ".join(f"{k}={v}" for k, v in alert["area"].items())
    when = f"{dt.date(st['year'], st['month'], 1):%B %Y}{' (so far)' if st['partial'] else ''}"
    ratio = f"{st['ratio']:.1f}× its historical median" if st["ratio"] is not None else "fire where the median is zero"
    subject = f"firex alert: {label} at {ratio}"
    body = (f"{label}\n{when}: {st['value']:.0f} fire cell-days, {ratio} ({st['median']:.0f}).\n"
            f"Your threshold: {alert['threshold']:.1f}×.\nSource: {st['source']}.\n")
    return subject, body


def _send_email(to: str, subject: str, body: str) -> str:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return "logged (SMTP_HOST not set)"
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.environ.get("ALERT_FROM", os.environ.get("SMTP_USER", "firex@localhost"))
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return "emailed"


def _send_webhook(url: str, subject: str, body: str, st: dict) -> str:
    data = json.dumps({"text": f"{subject}\n{body}", "status": st}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15):
        pass
    return "webhook sent"


def check_all(notify: bool = True) -> list[dict]:
    """Evaluate every alert; notify newly triggered ones (once per alert per month)."""
    with _lock:
        alerts = _load()
    results = []
    for alert in alerts:
        try:
            st = status(alert)
        except Exception as e:  # one broken area must not stop the others
            results.append({"alert": public(alert), "error": str(e)})
            continue
        sent: list[str] = []
        key = f"{st['year']}-{st['month']:02d}"
        if notify and st["triggered"] and alert.get("last_notified") != key:
            subject, body = _message(alert, st)
            log.warning("%s\n%s", subject, body)
            for target, fn in (("email", lambda: _send_email(alert["email"], subject, body)),
                               ("webhook", lambda: _send_webhook(alert["webhook"], subject, body, st))):
                if not alert.get(target):
                    continue
                try:
                    sent.append(fn())
                except Exception as e:
                    sent.append(f"{target} failed: {e}")
            if not sent:
                sent.append("logged")
            alert["last_notified"] = key
        results.append({"alert": public(alert), "status": st, "notified": sent})
    with _lock:  # persist last_notified without clobbering alerts added meanwhile
        latest = {a["id"]: a for a in alerts}
        merged = [{**a, "last_notified": latest.get(a["id"], a).get("last_notified")} for a in _load()]
        _save(merged)
    return results


def list_with_status() -> list[dict]:
    out = []
    for alert in _load():
        try:
            out.append({**public(alert), "status": status(alert)})
        except Exception as e:
            out.append({**public(alert), "status": None, "error": str(e)})
    return out


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for r in check_all():
        a, st = r["alert"], r.get("status")
        label = a.get("name") or a["area"]
        if st is None:
            print(f"✗ {label}: {r['error']}")
        else:
            flag = "TRIGGERED" if st["triggered"] else "ok"
            ratio = f"{st['ratio']}×" if st["ratio"] is not None else "n/a"
            print(f"{flag:9} {label}: {st['value']} cell-days, {ratio} median (threshold {a['threshold']}×) "
                  f"{'; '.join(r['notified'])}")
