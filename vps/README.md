# Gold Copier on a Linux VPS (no MetaApi)

Two MT5 terminals run 24/7 on one cheap Linux server:

```
MT5 Source (TMFinancials, investor login)          MT5 Target (Exness)
  GoldCopierMaster EA  ── shared file, every 0.1 s ──▶ GoldCopierSlave EA
  publishes open XAUUSD.f positions                    source open  → open XAUUSDm 0.01
                                                        source close → close that trade
                                                        Telegram message for every action
```

No SL, TP or trailing of its own. Trades already open longer than 120 s when the
copier sees them are skipped (so a restart never jumps into an old trade).

---

## 1. Rent the server (about €5/month)

1. Go to **https://contabo.com/en/vps/** and pick **Cloud VPS 10** (4 vCPU, 8 GB RAM).
2. Region: **European Union** (or United Kingdom). Image: **Ubuntu 24.04**.
3. Set a strong **root password**, pay, and wait for the email with the server **IP address**.

## 2. Connect and run the installer (one command)

On your Mac open **Terminal** and run (replace `YOUR_IP`):

```bash
ssh -L 5901:localhost:5900 root@YOUR_IP
```

Type `yes` the first time, then your root password. Inside that window run:

```bash
curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps/install.sh | bash
```

It asks you to choose a **VNC password** (6–8 characters), then installs everything.
**Keep this Terminal window open** for the whole setup — it is the secure tunnel to the screen.

## 3. Click through the MT5 installer

When the script says *ACTION NEEDED*:

1. On the Mac: **Finder → Go → Connect to Server…** → `vnc://localhost:5901` → Connect → enter the VNC password.
2. You see the server's screen with the MetaTrader 5 installer. Click **Next** and **Finish**.
3. If MT5 opens afterwards, close it. The script finishes by itself and starts **two** MT5 windows.

## 4. (Only if needed) open a terminal on the server screen

Right-click the desktop in VNC → **Terminals → xterm**. Not normally needed.

## 5. Log in both terminals

In each MT5 window: **File → Login to Trade Account**, tick **Save password**.

| Window | Server | Login | Password |
|---|---|---|---|
| title contains **MT5 Source** | `TMFinancials-Server` | 50861444 | **investor** (read-only) password |
| title contains **MT5 Target** | `Exness-MT5Real30` | 224085839 | **trading** password |

(If a "Open an account" wizard appears first, cancel it.)

## 6. Settings (both terminals)

**Tools → Options → Expert Advisors**:
- tick **Allow algorithmic trading**
- tick **Allow WebRequest for listed URL** and add `https://api.telegram.org` (needed on **MT5 Target**)

If the installer said the EAs were *not compiled*: press **F4** (MetaEditor), open
`Experts/GoldCopier/GoldCopierMaster.mq5` and `GoldCopierSlave.mq5`, press **F7** on each.

## 7. Attach the EAs

**MT5 Source**: open an **XAUUSD.f** chart → in Navigator, drag
**Expert Advisors → GoldCopier → GoldCopierMaster** onto it → OK.
The chart shows *"publishing N open XAUUSD.f position(s)"*.

**MT5 Target**: open an **XAUUSDm** chart → drag **GoldCopierSlave** onto it →
**Inputs** tab:

| Input | Value |
|---|---|
| `TelegramBotToken` | the bot token from **@BotFather** (`/mybots` → your bot → *API Token*) |
| `TelegramChatId` | your numeric id — send any message to **@userinfobot** in Telegram |
| `FixedLot` | `0.01` (default) |

Tick **Allow Algo Trading** in the *Common* tab → OK. Make sure the **Algo Trading**
button in the toolbar is green. You get a Telegram message: *"🟢 Gold copier started…"*.

Finally use **File → Exit** in both terminals once. They restart automatically within 30 s
with the EAs attached (this saves the setup so it survives reboots).

## 8. Test

Open one small **XAUUSD.f** trade on TMFinancials (this is a real trade on both accounts):
within a second you should get *"✅ Copied BUY XAUUSDm 0.01 …"*. Close it: *"✅ Closed …"*.

---

## Telegram messages you can receive

- ✅ Copied / ✅ Closed (with price and P/L)
- ⏭ Skipped (source trade was already old when seen)
- 🔴 Could not copy / close failed (with the broker's reason)
- 🔴 No live feed from TMFinancials for 60 s, 🔴 Exness disconnected for 60 s (and 🟢 when back)
- 🔴 Algo Trading is OFF
- ⚠️ Target closed outside the copier (manual close / stop-out) — it is not reopened
- 📊 Daily check at 07:00 GMT with balance and equity

## Maintenance

- See the screens any time: `ssh -L 5901:localhost:5900 root@YOUR_IP`, then `vnc://localhost:5901`.
- Restart everything: `systemctl restart gold-copier`
- Watchdog log: `cat /home/trader/watchdog.log`
- The server and both terminals start automatically after a reboot.
