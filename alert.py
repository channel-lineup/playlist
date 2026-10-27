"""Sports alert checker. Runs every 10 minutes from GitHub Actions.

Reads the combined guide (epg.xml.gz), finds LIVE games that match the keyword tiers below and
start 30-60 minutes from now, and sends one push per matchup listing every channel carrying it.
Also sends the one-time premium voucher warning on 2027-04-17.

Push service: Pushover (secrets PUSHOVER_TOKEN + PUSHOVER_USER), or ntfy (secret NTFY_TOPIC).
Already-sent alerts are remembered in alerts-sent.json, kept in the Actions cache (not committed).
"""
import gzip, json, os, re, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)
if os.environ.get("ALERT_TEST_NOW"):                     # for testing: pretend it's this UTC time
    NOW = datetime.strptime(os.environ["ALERT_TEST_NOW"], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
DRY_RUN = os.environ.get("ALERT_DRY_RUN") == "1"
WINDOW_MIN, WINDOW_MAX = 30, 60          # alert when kickoff is this many minutes away
GRACE_MIN = 20                           # if a run is delayed, still alert down to 20 minutes before
STATE_FILE = "alerts-sent.json"
# guide channels checked for alerts that aren't in the playlist (no stream exists for them): Des Moines locals + Cubs network
ALERT_ONLY = {"WOI-DT.us_locals1": "ABC 5 WOI (Des Moines)", "KCCI-DT.us_locals1": "CBS 8 KCCI (Des Moines)",
              "WHO-DT.us_locals1": "NBC 13 WHO (Des Moines)", "KDSM-DT.us_locals1": "FOX 17 KDSM (Des Moines)",
              "KCWI-DT.us_locals1": "The CW 23 KCWI (Des Moines)", "KFPX-DT.us_locals1": "ION KFPX (Des Moines)",
              "Marquee.Sports.Network.HD.us2": "Marquee Sports Network"}

def playlist_names(path="combined-tv.m3u"):
    """tvg-id -> list of channel names exactly as they appear in the playlist (and in TiviMate)."""
    names = {}
    for line in open(path, encoding="utf-8"):
        if line.startswith("#EXTINF"):
            cid = (re.search(r'tvg-id="([^"]*)"', line) or [None, ""])[1]
            nm = line.rstrip("\n").rsplit(",", 1)[-1].strip()
            if cid and cid != "none" and nm not in names.setdefault(cid, []):
                names[cid].append(nm)
    return names

# ---------------------------------------------------------------- keyword tiers
def has(text, *phrases):
    return any(re.search(p, text, re.I) for p in phrases)

# Iowa as a TEAM in the matchup line (not Iowa State / Northern Iowa / "Ames, Iowa" in a description)
IOWA_TEAM = r"(?<!Northern )(?<!, )\bIowa\b(?! State| Western| Wesleyan| Central| Wild| Cubs| Barnstormers)"
NOT_WORLD_SERIES = r"poker|climbing|darts|rodeo|beer pong|e-?sports|of fighting|cornhole|bowling|pickleball"

def tiers(title, sub, desc, match_line):
    text = " ".join(filter(None, [title, sub, desc]))
    hits = []
    # Baseball tier (OR): Chicago Cubs, or any World Series game
    if has(text, r"\bChicago Cubs\b") or has(match_line, r"\bCubs\b") \
       or (has(title + " " + sub + " " + desc[:200], r"\bWorld Series\b") and not has(text, NOT_WORLD_SERIES)):
        hits.append("Baseball")
    # WNBA & international tier: any Indiana Fever game; USA Women's Basketball / FIBA / other games
    # only when Caitlin Clark or Kelsey Mitchell is named in the listing
    if has(text, r"Indiana Fever") or has(text, r"Caitlin Clark", r"Kelsey Mitchell"):
        hits.append("WNBA/International")
    # Football tier (OR)
    if has(text, r"Green Bay Packers", r"\bPackers\b", r"Super Bowl"):
        hits.append("Football")
    # College & hockey tier (OR)
    if has(text, r"Stanley Cup", r"NHL Finals?", r"Hawkeyes?\b") or has(match_line, IOWA_TEAM):
        hits.append("College/Hockey")
    # Tournament bracket tier (OR)
    if has(text, r"March Madness", r"NCAA Tournament", r"NCAA Tourney", r"Final Four", r"NCAA .{0,30}Tournament",
           r"Sweet (16|Sixteen)", r"Elite Eight", r"First Four"):
        hits.append("Tournament")
    return hits

# ---------------------------------------------------------------- live-game filter
GAME_TITLE = re.compile(r"(?i)\b(football|baseball|basketball|hockey|wrestling|volleyball|softball|world series|super bowl|"
                        r"stanley cup|ncaa|march madness|final four)\b|\b(at|vs\.?|v)\b|\s@\s|\s x \s")
NOT_LIVE = re.compile(r"(?i)classic|countdown|pregame|pre-game|postgame|post-game|preview|review|highlights|kickoff show|"
                      r"\blive\b|today|tonight|\bfinal\b(?! four)|scoreboard|picks|matchup|in 60|rewind|replay|re-air|"
                      r"encore|recap|show|podcast|talk|insider|report|weekly|magazine|film|draft|fantasy|huddle|"
                      r"central|zone|special|documentary|story|history|greatest|best of|top 10|hour\b|champions|"
                      r"reunion|360|presents|throwback|bitesize|in-game|good morning|news")
REPLAY_DESC = re.compile(r"(?i)^\(?(19|20)\d\d\)?[:\s]|\bre-?air|\breplay\b|\bencore\b|\brebroadcast|from (19|20)\d\d\b|"
                         r"verslag|samenvatting|gespeeld|resumen|reprise|melhores momentos|compacto|^vt\b|\bvt -")

LIVE_PREFIX = re.compile(r"(?i)^(live|en vivo|en directo|ao vivo)\b\s*[:\-]?\s*")
LIVE_SUFFIX = re.compile(r"(?i)\s*[-:(]\s*(live|en vivo|en directo|ao vivo)\)?\s*$")
def clean_title(title):
    return LIVE_SUFFIX.sub("", LIVE_PREFIX.sub("", title.strip())).strip()

def is_live_game(prog):
    raw = (prog.findtext("title") or "").strip()
    title = clean_title(raw)
    desc = (prog.findtext("desc") or "").strip()
    flagged_live = prog.find("live") is not None or bool(LIVE_PREFIX.match(raw) or LIVE_SUFFIX.search(raw))
    if prog.find("previously-shown") is not None and not flagged_live:
        return False
    if REPLAY_DESC.search(desc[:120]) or re.search(r"\b(19[5-9]\d|20[0-2][0-5])\b", title):
        return False                                   # old-game replays, re-airs, "1996 Champions" documentaries
    if NOT_LIVE.search(title) or re.match(r"(?i)^vt\b", raw):
        return False
    if GAME_TITLE.search(title):
        return True
    # e.g. Sky "Live NFL" + "Green Bay Packers v Carolina Panthers (Kick-off 12.15am)"
    first = re.split(r"(?<=[.!?])\s", (desc.splitlines() or [""])[0], 1)[0]
    return flagged_live and bool(re.search(r"\b(at|vs\.?|v)\b|\s@\s", first)) and len(first) < 120

def matchup(prog):
    title = clean_title(prog.findtext("title") or "")
    sub = (prog.findtext("sub-title") or "").strip()
    desc = (prog.findtext("desc") or "").strip()
    first = (desc.splitlines() or [""])[0]
    sent1 = re.sub(r"\s*\(kick-?off[^)]*\)\.?", "", re.split(r"(?<=[.!?])\s", first, 1)[0], flags=re.I).rstrip(".")
    for cand in (sub, first, sent1):
        if re.search(r"\b(at|vs\.?|v\.?|@)\b", cand) and len(cand) < 90:
            return cand
    sentence = re.split(r"(?<=[.!?])\s", desc, 1)[0] if desc else ""
    if sentence and len(sentence) < 120:
        return f"{title} - {sentence}"
    return f"{title}: {sub}" if sub else title

def team_words(text):
    stop = {"The", "At", "Vs", "Game", "Football", "Basketball", "Baseball", "Hockey", "College", "NFL", "MLB", "NHL",
            "NBA", "WNBA", "NCAA", "World", "Series", "Live", "Field", "Stadium", "Arena", "Park", "Center"}
    return {w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", text) if w not in stop}

# ---------------------------------------------------------------- push
def push(title, message):
    if DRY_RUN:
        print("DRY RUN ->", title, "|", message.replace("\n", " | ")); return True
    tok, user, topic = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER"), os.environ.get("NTFY_TOPIC")
    try:
        if tok and user:
            data = urllib.parse.urlencode({"token": tok, "user": user, "title": title, "message": message,
                                           "priority": 1, "sound": "siren"}).encode()
            urllib.request.urlopen("https://api.pushover.net/1/messages.json", data, timeout=30)
        elif topic:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                         headers={"Title": title.encode("utf-8"), "Priority": "5", "Tags": "rotating_light"})
            urllib.request.urlopen(req, timeout=30)
        else:
            print("No push service configured (PUSHOVER_TOKEN/PUSHOVER_USER or NTFY_TOPIC)."); return False
        return True
    except Exception as ex:
        print("push failed:", ex); return False

# ---------------------------------------------------------------- main
def parse_time(s):
    dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    off = s[14:].strip()                                   # e.g. "+0000" or "-0500"
    if re.fullmatch(r"[+-]\d{4}", off):
        delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        dt = dt - delta if off[0] == "+" else dt + delta
    return dt

from zoneinfo import ZoneInfo
def stated_kickoff(prog, start):
    """Use 'Kick-off 12.15am' style times from the description when present (UK listings)."""
    m = re.search(r"(?i)kick-?off:?\s*(\d{1,2})[.:](\d{2})\s*(am|pm)?", prog.findtext("desc") or "")
    if not m: return start
    tz = ZoneInfo("Europe/London") if prog.get("channel", "").endswith(".uk") else timezone.utc
    local = start.astimezone(tz)
    h, mi = int(m.group(1)) % 12 if m.group(3) else int(m.group(1)), int(m.group(2))
    if (m.group(3) or "").lower() == "pm": h += 12
    k = local.replace(hour=h, minute=mi, second=0)
    if k < local - timedelta(hours=1): k += timedelta(days=1)      # kickoff after midnight
    return k.astimezone(timezone.utc) if abs((k - local).total_seconds()) < 6 * 3600 else start

def find_events(root, lo_min, hi_min, now=None):
    """Matching live games starting lo_min..hi_min minutes from now. Returns {key: event}, channels as tvg-ids."""
    now = now or NOW
    events = {}
    for p in root.iter("programme"):
        start = parse_time(p.get("start", ""))
        if (start - now).total_seconds() / 60 > hi_min + 360 or not is_live_game(p):
            continue
        start = stated_kickoff(p, start)
        mins = (start - now).total_seconds() / 60
        if not (lo_min <= mins <= hi_min):
            continue
        title, sub, desc = p.findtext("title") or "", p.findtext("sub-title") or "", p.findtext("desc") or ""
        m = matchup(p)
        hit = tiers(clean_title(title), sub, desc, m + " " + sub)
        if not hit:
            continue
        slot = start.replace(minute=start.minute // 15 * 15, second=0)
        key = f"{slot:%Y%m%d%H%M}|{'+'.join(sorted(hit))}"
        ev = events.setdefault(key, {"matchup": m, "start": start, "channels": [], "tiers": hit})
        if (re.search(r"\b(at|vs\.?|@)\b", m) and len(m) < len(ev["matchup"])) or not re.search(r"\b(at|vs\.?|@)\b", ev["matchup"]):
            ev["matchup"] = m                              # prefer the short "Team A at Team B" wording
        if p.get("channel") not in ev["channels"]:
            ev["channels"].append(p.get("channel"))
    return events

def main():
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    in_list = playlist_names()
    def label(cid):
        if cid in in_list:
            return " / ".join(in_list[cid])               # exact TiviMate channel name(s)
        return f"{ALERT_ONLY.get(cid, cid)} (not in playlist)"
    root = ET.parse(gzip.open("epg.xml.gz")).getroot()
    events = find_events(root, GRACE_MIN, WINDOW_MAX)

    recent = state.get("_recent", [])                   # [start_iso, team words] of recent alerts
    for key, ev in sorted(events.items(), key=lambda kv: kv[1]["start"]):
        if key in state:
            continue
        words = team_words(ev["matchup"])
        if words and any(len(words & set(w)) >= 2 and abs((ev["start"] - datetime.fromisoformat(t)).total_seconds()) < 12 * 3600
                         for t, w in recent):
            state[key] = "duplicate-airing"; continue  # same game re-airing on another channel/time
        chans = sorted(ev["channels"], key=lambda c: c not in in_list)
        msg = f"Matchup Detected: {ev['matchup']}\nBroadcast Channel: {', '.join(label(c) for c in chans)}"
        if push("🚨 Time-Sensitive Master Sports Alert", msg):
            state[key] = NOW.isoformat()
            recent.append([ev["start"].isoformat(), sorted(words)])
    state["_recent"] = [r for r in recent if (NOW - datetime.fromisoformat(r[0])).total_seconds() < 24 * 3600]

    # premium voucher: 180 days from 2026-10-26 -> expires 2027-04-24; warn on 2027-04-17 at 9:00 AM Central
    if NOW >= datetime(2027, 4, 17, 14, 0, tzinfo=timezone.utc) and "voucher-2027-04-17" not in state:
        if push("🚨 Time-Sensitive Subscription Alert",
                "Premium streaming server voucher expires: April 24, 2027\nStatus: 7 days remaining - manual top-up required"):
            state["voucher-2027-04-17"] = NOW.isoformat()

    cutoff = (NOW - timedelta(days=3)).strftime("%Y%m%d%H%M")
    state = {k: v for k, v in state.items() if k.startswith(("voucher", "_")) or k[:12] >= cutoff}
    json.dump(state, open(STATE_FILE, "w"), indent=0)
    print(f"checked {NOW:%Y-%m-%d %H:%M} UTC: {len(events)} matching event(s) in window")

if __name__ == "__main__":
    main()
