#!/usr/bin/env python3
"""Local catch-all SMTP server for testing the alert email (127.0.0.1:2525). Saves mails to e2e_out/mail.log.
Needs Python 3.9-3.11 (smtpd/asyncore were removed in 3.12)."""
import asyncore
import smtpd
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "e2e_out" / "mail.log"
OUT.parent.mkdir(exist_ok=True)


class Catch(smtpd.SMTPServer):
    def process_message(self, peer, mailfrom, rcpttos, data, **kw):
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        with OUT.open("a") as f:
            f.write(f"=== MAIL from={mailfrom} to={rcpttos}\n{data}\n\n")
        print("mail captured", rcpttos, flush=True)


Catch(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 2525), None)
print("fake SMTP on 127.0.0.1:2525", flush=True)
asyncore.loop()
