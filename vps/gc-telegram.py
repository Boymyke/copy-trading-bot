#!/usr/bin/env python3
"""Gold Copier Telegram helper (runs on the VPS as a systemd service).

MT5 under Wine does not keep its WebRequest allow-list reliably, so the slave EA
never talks to Telegram itself:
  * EA -> Telegram: the EA appends one message per line to gold_copier_outbox.txt
    (newlines escaped as \\n); this helper sends them and empties the file.
  * Telegram -> EA: /lot and /per commands from your chat are written to
    gold_copier_inbox.txt; the EA applies them at start-up and at the morning check.
Usage: gc-telegram.py <MT5 Common/Files folder>
"""
import json, os, sys, time, urllib.parse, urllib.request

FILES = sys.argv[1]
OUT = os.path.join(FILES, "gold_copier_outbox.txt")
SENDING = OUT + ".sending"
INBOX = os.path.join(FILES, "gold_copier_inbox.txt")
CFG = os.path.join(FILES, "gold_copier_telegram.txt")
OFFSET = os.path.expanduser("~/.gc-telegram-offset")
POLL_EVERY = 300  # seconds between checks for your /lot commands (they apply at the morning check)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def config():
    tok = chat = ""
    try:
        for line in open(CFG, encoding="utf-8", errors="replace"):
            k, _, v = line.strip().partition("=")
            if k == "token": tok = v.strip()
            if k == "chat": chat = v.strip()
    except OSError:
        pass
    return tok, chat


def api(tok, method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/{method}", data=data, timeout=30) as r:
        return json.load(r)


def unescape(line):
    out, i = [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            n = line[i + 1]
            out.append("\n" if n == "n" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def send_outbox(tok, chat):
    if not os.path.exists(SENDING):
        if not os.path.exists(OUT) or os.path.getsize(OUT) == 0:
            return
        os.replace(OUT, SENDING)
        time.sleep(0.5)  # let a write that was in progress finish
    with open(SENDING, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip("\r\n") for l in f if l.strip()]
    for i, line in enumerate(lines):
        try:
            api(tok, "sendMessage", chat_id=chat, text=unescape(line)[:4000])
        except Exception as e:
            log("send failed, will retry:", e)
            with open(SENDING, "w", encoding="utf-8") as f:
                f.write("".join(l + "\n" for l in lines[i:]))
            return
    os.remove(SENDING)


HELP = ("This chat receives the trading robot's updates: every trade opened and closed with its profit or loss, "
        "plus a daily summary. No commands are needed.")


def poll_commands(tok, chat):
    try:
        offset = int(open(OFFSET).read().strip())
    except (OSError, ValueError):
        offset = 0
    res = api(tok, "getUpdates", offset=offset, timeout=0, allowed_updates='["message"]')
    for u in res.get("result", []):
        offset = max(offset, u["update_id"] + 1)
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != chat:
            continue
        text = (m.get("text") or "").strip()
        cmd = text.lower().lstrip("/").split("@")[0].split()
        if cmd and cmd[0] in ("lot", "per") and len(text.split()) >= 2:
            with open(INBOX, "a", encoding="utf-8") as f:
                f.write(text.replace("\n", " ") + "\n")
            api(tok, "sendMessage", chat_id=chat,
                text=f"👍 Got \"{text}\". It will be applied at the next morning check (8:00 am Lagos) or copier restart.")
        elif text:
            api(tok, "sendMessage", chat_id=chat, text=HELP)
    with open(OFFSET, "w") as f:
        f.write(str(offset))


def main():
    log("gc-telegram started, files:", FILES)
    last_poll = 0
    while True:
        tok, chat = config()
        if tok and chat:
            try:
                send_outbox(tok, chat)
            except Exception as e:
                log("outbox error:", e)
            if time.time() - last_poll >= POLL_EVERY:
                last_poll = time.time()
                try:
                    poll_commands(tok, chat)
                except Exception as e:
                    log("command poll error:", e)
        time.sleep(1)


if __name__ == "__main__":
    main()
