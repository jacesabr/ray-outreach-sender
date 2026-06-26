"""Read bounce-backs from the Gmail inbox and mark those addresses 'bounced'
in Neon, so they stop counting as reached and never get re-sent. Reports the
current bounce rate. Run periodically (or as a cron)."""
import os, imaplib, email, re, psycopg

ADDR = os.environ["GMAIL_ADDRESS"]
PW = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
DSN = os.environ["DATABASE_URL"]

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

def extract_failed(msg):
    """Return the set of failed recipient addresses from a bounce message."""
    addrs = set()
    xfr = msg.get("X-Failed-Recipients")
    if xfr:
        addrs.update(a.strip().lower() for a in xfr.split(",") if "@" in a)
    # also scan the delivery-status / body for Final-Recipient lines
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("message/delivery-status", "text/plain"):
            try:
                txt = part.get_payload(decode=True).decode("utf-8", "replace")
            except Exception:
                continue
            for line in txt.splitlines():
                if "final-recipient" in line.lower() or "550" in line:
                    for m in EMAIL_RE.findall(line):
                        a = m.lower()
                        if a != ADDR:
                            addrs.add(a)
    return addrs

print("connecting to IMAP...")
M = imaplib.IMAP4_SSL("imap.gmail.com")
M.login(ADDR, PW)
M.select("INBOX")

bounced = set()
for query in ['(FROM "mailer-daemon")', '(FROM "postmaster")',
              '(SUBJECT "Delivery Status Notification")',
              '(SUBJECT "Undelivered Mail Returned")']:
    typ, data = M.search(None, query)
    if typ != "OK" or not data or not data[0]:
        continue
    for i in data[0].split():
        typ, md = M.fetch(i, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(md[0][1])
        bounced |= extract_failed(msg)
M.logout()
print(f"found {len(bounced)} bounced address(es) in the inbox")

with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    marked = 0
    for a in bounced:
        cur.execute("UPDATE contacts SET status='bounced', detail='hard bounce (550)' "
                    "WHERE lower(email)=%s AND status <> 'bounced'", (a,))
        marked += cur.rowcount
    conn.commit()
    cur.execute("SELECT count(*) FILTER (WHERE status='sent'), "
                "count(*) FILTER (WHERE status='bounced') FROM contacts")
    sent, bnc = cur.fetchone()

rate = round(100 * bnc / (sent + bnc), 1) if (sent + bnc) else 0
print(f"marked {marked} as bounced. now: sent={sent}, bounced={bnc}, bounce rate={rate}%")
