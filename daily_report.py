"""Daily status email for the Ray outreach campaign.
Runs once a day (Render cron), reads stats from Neon, emails a summary so you
know it's still running. Sends nothing to contacts — only to REPORT_TO.

ENV: DATABASE_URL, GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
     REPORT_TO (default = GMAIL_ADDRESS), DAILY_CAP (default 100)
"""
import os, ssl, smtplib, psycopg
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

DSN = os.environ["DATABASE_URL"]
FROM = os.environ["GMAIL_ADDRESS"]
PW = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
TO = os.environ.get("REPORT_TO", FROM)
CAP = int(os.environ.get("DAILY_CAP", "100"))

with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT
          count(*) FILTER (WHERE status='sent'),
          count(*) FILTER (WHERE status='sent' AND sent_at >= now() - interval '24 hours'),
          count(*) FILTER (WHERE status='pending'),
          count(*) FILTER (WHERE status='failed'),
          count(*)
        FROM contacts
    """)
    sent_total, sent_24h, pending, failed, total = cur.fetchone()

days_left = (pending + CAP - 1) // CAP if CAP else 0
pct = round(100 * sent_total / total, 1) if total else 0
body = f"""Ray outreach - daily status  ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})

Reached in last 24h:  {sent_24h}
Reached total:        {sent_total} of {total}  ({pct}%)
Still pending:        {pending}
Failed:               {failed}

At ~{CAP}/day, about {days_left} days left.
The sender is running. To pause it, suspend the Render cron 'ray-outreach-sender'.
"""

msg = MIMEText(body, "plain", "utf-8")
msg["Subject"] = f"Ray outreach: {sent_24h} reached today ({sent_total}/{total} total)"
msg["From"] = formataddr(("Ray Outreach Bot", FROM))
msg["To"] = TO

ctx = ssl.create_default_context()
with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
    s.starttls(context=ctx)
    s.login(FROM, PW)
    s.send_message(msg)

print(f"daily report sent to {TO}: 24h={sent_24h} total={sent_total}/{total} pending={pending}")
