"""Outgoing email (password resets). Uses SMTP when configured.

Without SMTP settings, development mode writes the message to the server log so the
flow can be tested locally. Production never logs reset links.
"""

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage

import config

log = logging.getLogger("leadscout.mail")


def configured():
    return bool(config.SMTP_HOST and config.MAIL_FROM)


def _send(msg):
    try:
        ctx = ssl.create_default_context()
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
            server.starttls(context=ctx)
        with server:
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        log.info("email sent", extra={"event": "email_sent"})
    except Exception as exc:  # never crash a request because mail failed
        log.error("email failed: %s", type(exc).__name__, extra={"event": "email_failed"})


def send(to, subject, text, html=None):
    if not configured():
        if config.IS_PROD:
            log.error("email not sent: SMTP is not configured", extra={"event": "email_unconfigured"})
        else:
            log.warning("DEV MAIL (SMTP not configured)\nTo: %s\nSubject: %s\n\n%s", to, subject, text)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.MAIL_FROM, to, subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    threading.Thread(target=_send, args=(msg,), daemon=True).start()
    return True
