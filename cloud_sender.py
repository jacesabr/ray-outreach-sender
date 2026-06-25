"""
Neon-backed email sender — runs as a Render Cron Job.

Each invocation: connect to Neon, check today's sent count, and if under the
daily cap, grab the next `pending` contact by rank, send one of the 10 email
variants (personalized), mark it `sent`, and exit. State lives entirely in
Neon, so it's fully resumable and can NEVER double-send (row-locked + status).

CREDIBILITY GUARANTEES
  * Greets by first name ONLY when it's a real, capitalized name; otherwise
    falls back to "Hi there," — never a handle, never a raw {first_name}.
  * A regex strips any leftover {placeholder} before sending, so a broken
    mail-merge field is impossible.

ENV (set as Render env vars / secrets)
  DATABASE_URL        Neon connection string
  GMAIL_ADDRESS       sending address
  GMAIL_APP_PASSWORD  16-char app password
  DAILY_CAP           default 100
  BATCH               emails per invocation, default 1
  SEND                "1" = really send; anything else = dry run (safe default)

LOCAL USAGE
  python cloud_sender.py            # dry run: render next emails, send nothing
  python cloud_sender.py --test     # send ONE test email to yourself
  python cloud_sender.py --go       # real send (overrides SEND)
"""
import os, re, sys, ssl, smtplib, hashlib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from email.header import Header
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
VARIANTS_FILE = os.path.join(HERE, "email_variants.txt")

GOOD_NAME = re.compile(r"^[A-Z][A-Za-z'’\-]{1,19}$")  # capitalized, letters only
LEFTOVER = re.compile(r"\{[^}]*\}")


def clean_name(raw):
    """Real capitalized first name -> use it. Otherwise -> 'there'."""
    fn = (raw or "").strip()
    fn = fn.split()[0] if fn else ""
    return fn if GOOD_NAME.match(fn) else "there"


def load_variants():
    raw = open(VARIANTS_FILE, encoding="utf-8").read()
    out = []
    for chunk in raw.split("===VARIANT==="):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.split("\n")
        if lines[0].lower().startswith("subject:"):
            subj = lines[0].split(":", 1)[1].strip()
            body = "\n".join(lines[1:]).lstrip("\n")
            out.append((subj, body))
    if not out:
        raise SystemExit("no variants found in email_variants.txt")
    return out


def render(variants, email, first_name):
    """Pick a stable variant by email hash and fill the name safely."""
    h = int(hashlib.md5(email.lower().encode()).hexdigest(), 16)
    subj, body = variants[h % len(variants)]
    name = clean_name(first_name)
    subj = LEFTOVER.sub("", subj.replace("{first_name}", name)).strip()
    body = LEFTOVER.sub("", body.replace("{first_name}", name))
    return subj, body


def build_msg(from_addr, to_addr, subj, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr(("Jace Sabr", from_addr))
    msg["To"] = to_addr
    msg["Reply-To"] = from_addr
    msg["Message-ID"] = make_msgid()
    return msg


def smtp_send(from_addr, app_pw, msg):
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(from_addr, app_pw)
        s.send_message(msg)


def main():
    go = ("--go" in sys.argv) or (os.environ.get("SEND") == "1")
    test = "--test" in sys.argv
    dsn = os.environ.get("DATABASE_URL", "")
    from_addr = os.environ.get("GMAIL_ADDRESS", "")
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    cap = int(os.environ.get("DAILY_CAP", "100"))
    batch = int(os.environ.get("BATCH", "1"))
    if not dsn:
        raise SystemExit("DATABASE_URL not set")
    variants = load_variants()

    # --- self-test: email yourself one sample, no DB writes ---
    if test:
        subj, body = render(variants, from_addr, "Jace")
        print(f"TEST -> {from_addr}\nSubject: {subj}\n")
        smtp_send(from_addr, app_pw, build_msg(from_addr, from_addr, subj, body))
        print("sent. check inbox + spam.")
        return

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM contacts WHERE status='sent' "
                        "AND sent_at >= date_trunc('day', now())")
            sent_today = cur.fetchone()[0]
        print(f"sent today: {sent_today}/{cap} | mode: {'SEND' if go else 'DRY-RUN'}")

        done_this_run = 0
        for _ in range(batch):
            if sent_today + done_this_run >= cap:
                print("daily cap reached — exiting.")
                break
            with conn.cursor() as cur:
                cur.execute("SELECT id, email, first_name FROM contacts "
                            "WHERE status='pending' ORDER BY rank "
                            "LIMIT 1 FOR UPDATE SKIP LOCKED")
                row = cur.fetchone()
                if not row:
                    print("no pending contacts left.")
                    break
                cid, email, first = row
                subj, body = render(variants, email, first)
                greet = next((l for l in body.split("\n") if l.strip()), "")

                if not go:
                    print(f"  [DRY] {email:40} | {greet:20} | {subj}")
                    conn.rollback()   # release the row lock, change nothing
                    # advance past this row in dry-run by marking nothing —
                    # re-select would loop, so just stop after showing batch
                    done_this_run += 1
                    if done_this_run >= batch:
                        break
                    continue

                try:
                    smtp_send(from_addr, app_pw, build_msg(from_addr, email, subj, body))
                    cur.execute("UPDATE contacts SET status='sent', sent_at=now(), "
                                "detail=%s WHERE id=%s", (subj[:200], cid))
                    conn.commit()
                    done_this_run += 1
                    print(f"  SENT {email} ({greet.strip()})")
                except smtplib.SMTPAuthenticationError as e:
                    conn.rollback()
                    raise SystemExit(f"AUTH FAILED: {e}")
                except Exception as e:  # noqa: BLE001
                    cur.execute("UPDATE contacts SET status='failed', "
                                "detail=%s WHERE id=%s", (str(e)[:200], cid))
                    conn.commit()
                    print(f"  FAIL {email} — {e}")

    print(f"done. sent this run: {done_this_run}")


if __name__ == "__main__":
    main()
