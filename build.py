"""Rebuilds combined-tv.m3u daily: merges a US-sports subset of iptv-org, the Free-TV playlist
and the full iptv-org playlist (duplicates kept), and remaps tvg-ids to the user's EPG sources."""
import re, sys, json, gzip, io, unicodedata, urllib.request
from collections import defaultdict
from rapidfuzz import process, fuzz

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=300).read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data

FREETV = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
IPTVORG = "https://iptv-org.github.io/iptv/index.m3u"
IPTVEPG = "https://iptv-epg.org/files/epg-us.xml.gz"
SHARE_INDEX = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.txt"

index_txt = get(IPTVORG).decode("utf-8", "replace")
freetv_txt = get(FREETV).decode("utf-8", "replace")
xml = get(IPTVEPG).decode("utf-8", "replace")
iptvepg_rows = re.findall(r'<channel id="([^"]*)">\s*<display-name[^>]*>([^<]*)</display-name>', xml)
share_lines = get(SHARE_INDEX).decode("utf-8", "replace").splitlines()
print("sources:", len(index_txt), len(freetv_txt), len(iptvepg_rows), len(share_lines))
if len(iptvepg_rows) < 1000 or len(share_lines) < 1000 or index_txt.count("#EXTINF") < 5000:
    sys.exit("A source looks broken; keeping the previous playlist.")

# ---- US sports subset (same rules as the original us-sports.m3u) ----
groups=[
("US Sports Networks",r"^(ESPN|ESPN2|ESPNU|ESPNDeportes|ESPN8TheOcho)\.us@|^(FoxSports1|FoxSports2|FoxDeportes|NFLNetwork|NBATV|MLB|NHLNetwork|ACCNetwork|MSG|USANetwork|TennisChannel|TennisChannel2|beINSPORTSXTRA|beINSPORTSXTRAenEspanol)\.us@"),
("US Broadcast Networks",r"^(ABC|CBS|NBC|Fox|TNT|TBS|truTV|IONTV|Telemundo|Univision|UniMas)\.us@"),
("League & Team Channels",r"^(NFLChannel|MLBStrikeZone|NBCSportsNOW|CBSSportsHQ|Pac12Insider|ACCDigitalNetwork|Stadium|BEKSports|PGATour|NHRATV|PBRRidePass|WWENetwork|PFLMMA|FuboSportsNetwork|SportsGrid|DraftKingsNetwork|FloHockey|FloRacing|Overtime|LacrosseTV)\.us@|^HardKnocks\.ca@"),
("International - US Leagues",r"^SkySportsNFL\.uk@|^(TSN4|TSNTheOcho|CTV)\.ca@|^ESPN[0-9]?(LatinAmerica|Extra)LatinAmerica?\.us@|^ESPN(Extra)?LatinAmerica\.us@|^ESPN[0-9]LatinAmerica\.us@|^ESPN[0-9]?\.(br|nl)@|^ESPNPremium\.(ar|cl)@|^DSports2?\.us@|^FoxSports2?\.ar@|^ClaroSports\.mx@|^AztecaDeportesNetwork\.mx@"),
]
def sports_subset(text):
    L = text.splitlines(); out = []; seen = set(); i = 0
    while i < len(L):
        l = L[i]
        if not l.startswith("#EXTINF"): i += 1; continue
        j = i + 1; extra = []
        while j < len(L) and L[j].startswith("#"): extra.append(L[j]); j += 1
        url = L[j] if j < len(L) else ""
        tid = (re.search(r'tvg-id="([^"]*)"', l) or [None, ""])[1]
        if not re.search(r"news|weather|business|soul|novela|series|comedy|vault|radio", tid, re.I):
            for g, p in groups:
                if re.search(p, tid) and url not in seen:
                    seen.add(url)
                    nl = re.sub(r'group-title="[^"]*"', f'group-title="{g}"', l) if 'group-title="' in l else l.replace(",", f' group-title="{g}",', 1)
                    out += [nl] + extra + [url]; break
        i = j + 1
    return "#EXTM3U\n" + "\n".join(out) + "\n"

sports_txt = sports_subset(index_txt)

SHARE_SECTIONS = ["US_SPORTS1", "US2", "US_LOCALS1"]   # priority order after iptv-epg.org
SRC_RANK = {"iptv-epg.org": 0, "US_SPORTS1": 1, "US2": 2, "US_LOCALS1": 3}
NOISE = r"\b(hd|sd|fhd|uhd|4k|tv|channel|the|east|feed|alt|alternate|dt|network|live|us|usa)\b"

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("+", " plus ").replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(NOISE, " ", s)
    return re.sub(r"\s+", " ", s).strip()

epg = []  # (id, source, country, normname)
ids_exact = {}
for cid, dn in iptvepg_rows:
    suf = cid.rsplit(".", 1)[-1].lower() if "." in cid else ""
    country = suf if len(suf) == 2 else "*"
    dn2 = re.sub(r"^[A-Z0-9+]{2,5} - ", "", dn)  # drop "US - " prefix
    if suf in ("sxm",): continue  # radio feeds
    e = (cid, "iptv-epg.org", country, norm(dn2))
    epg.append(e); ids_exact[cid.lower()] = e
section = None
for line in share_lines:
    line = line.strip()
    m = re.match(r"^-- epg_ripper_(.+) --$", line)
    if m: section = m.group(1); continue
    if section not in SHARE_SECTIONS or not line or "dummy" in line.lower(): continue
    epg.append((line, section, "us", norm(line.rsplit(".", 1)[0].replace(".", " "))))

by_country = defaultdict(list); exact = defaultdict(list); callsign = defaultdict(list)
for e in epg:
    by_country[e[2]].append(e); exact[(e[2], e[3])].append(e)
    if e[2] == "us":
        for cs in re.findall(r"(?<![A-Z])([KW][A-Z]{2,3})(?![A-Z])", e[0].replace("_", " ").replace("-", " ").replace(".", " ")):
            callsign[cs].append(e)
pick = lambda c: sorted(c, key=lambda e: (SRC_RANK[e[1]], len(e[0])))[0]

def match(name, tvgid, country):
    # 1. same id already exists in iptv-epg.org once the iptv-org "@feed" suffix is dropped
    base = tvgid.split("@")[0].lower()
    if base and base in ids_exact: return ids_exact[base], "id"
    clean = re.sub(r"\(.*?\)|\[.*?\]|[ⓈⓉⓎⒼⓋ]", "", name)
    # 2. US local station call sign (feed suffix or name)
    if country == "us":
        for cs in re.findall(r"(?<![A-Z])([KW][A-Z]{2,3})(?:-?(?:DT|TV|CD|LD))?(?![A-Z])", (tvgid.split("@")[-1] if "@" in tvgid else "") + " " + clean):
            if cs in callsign: return pick(callsign[cs]), "callsign"
    n = norm(clean)
    if not n: return None, None
    for c in ((country, "*") if country == "us" else (country,)):
        if (c, n) in exact: return pick(exact[(c, n)]), "name"
    for c in (country,):
        pool = by_country.get(c, [])
        if not pool: continue
        best = process.extractOne(n, [e[3] for e in pool], scorer=fuzz.token_sort_ratio, score_cutoff=93)
        if best:
            cand = pool[best[2]]
            if re.findall(r"\d+", n) == re.findall(r"\d+", cand[3]) and n.split()[0] == cand[3].split()[0]:
                return cand, "fuzzy"
    return None, None

# ---- category (group-title) cleanup ----
from collections import Counter
SPORTS_GROUPS = {"US Sports Networks": "🏈 US Sports Networks", "US Broadcast Networks": "📺 US Broadcast Networks",
                 "League & Team Channels": "🏆 League & Team Channels", "International - US Leagues": "🌎 International - US Leagues"}
RENAME = {"UK": "United Kingdom", "USA": "United States", "日本 / Japan": "Japan", "Korea": "South Korea",
          "Trinidad": "Trinidad and Tobago", "Undefined": "Other", "News (AR)": "News (Arabic)", "News (ES)": "News (Spanish)",
          "Documentaries (AR)": "Documentary (Arabic)", "Documentaries (EN)": "Documentary", "Music (EN)": "Music",
          "VOD Movies (EN)": "Movies (On Demand)", "VOD Italy": "Italy (On Demand)"}
CATEGORY_WORDS = ("News", "Business", "Weather", "Music", "Documentary", "Movies")
def flag(code):
    code = (code or "").upper()
    if code == "UK": code = "GB"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code) if re.fullmatch(r"[A-Z]{2}", code) else ""
ft_codes = defaultdict(Counter)
for l in freetv_txt.splitlines():
    if l.startswith("#EXTINF"):
        g = (re.search(r'group-title="([^"]*)"', l) or [None, ""])[1]
        c = (re.search(r'tvg-country="([^"]*)"', l) or [None, ""])[1].split(";")[0]
        if c: ft_codes[g][c] += 1
def clean_group(label, g):
    if label == "Sports": return SPORTS_GROUPS.get(g, g)
    if label == "iptv-org":
        g = g.split(";")[0].strip() or "Other"
        return RENAME.get(g, g)
    name = RENAME.get(g, g)
    if name.startswith(CATEGORY_WORDS): return name
    code = ft_codes[g].most_common(1)[0][0] if ft_codes[g] else {"Lebanon": "LB", "VOD Italy": "IT"}.get(g, "")
    f = flag(code)
    return f"{f} {name}" if f else name

out = ["#EXTM3U"]; stats = defaultdict(lambda: defaultdict(int)); by_src = defaultdict(int)
samples = defaultdict(list); unmatched_us = []; cache = {}
for label, text in [("Sports", sports_txt), ("Free-TV", freetv_txt), ("iptv-org", index_txt)]:
    L = text.splitlines()
    i = 0
    while i < len(L):
        l = L[i]
        if not l.startswith("#EXTINF"): i += 1; continue
        j = i + 1; extra = []
        while j < len(L) and L[j].startswith("#"): extra.append(L[j]); j += 1
        url = L[j] if j < len(L) else ""
        tid = (re.search(r'tvg-id="([^"]*)"', l) or [None, ""])[1]
        name = l.rsplit(",", 1)[-1].strip()
        tc = (re.search(r'tvg-country="([^"]*)"', l) or [None, ""])[1].lower()
        m = re.search(r"\.([a-z]{2})(@|$)", tid)
        country = m.group(1) if m else (tc.split(";")[0] if tc else "")
        key = (name, tid, country)
        if key not in cache: cache[key] = match(name, tid, country)
        e, how = cache[key]
        stats[label]["total"] += 1
        if country == "us": stats[label]["us_total"] += 1
        if e:
            stats[label]["matched"] += 1; by_src[e[1]] += 1
            if country == "us": stats[label]["us_matched"] += 1
            l = re.sub(r'tvg-id="[^"]*"', f'tvg-id="{e[0]}"', l) if 'tvg-id="' in l else l.replace("#EXTINF:-1", f'#EXTINF:-1 tvg-id="{e[0]}"', 1)
            if len(samples[how]) < 25: samples[how].append(f"{name}  ->  {e[0]}")
        elif country == "us" and len(unmatched_us) < 300:
            unmatched_us.append(name)
        g = (re.search(r'group-title="([^"]*)"', l) or [None, "Other"])[1]
        ng = clean_group(label, g)
        l = re.sub(r'group-title="[^"]*"', f'group-title="{ng}"', l) if 'group-title="' in l else l.replace(",", f' group-title="{ng}",', 1)
        out += [l] + extra + [url]
        i = j + 1

urls = ["https://iptv-epg.org/files/epg-us.xml.gz"] + [f"https://epgshare01.online/epgshare01/epg_ripper_{s}.xml.gz" for s in ["US2", "US_LOCALS1", "US_SPORTS1"]]
out[0] = f'#EXTM3U x-tvg-url="{",".join(urls)}"'
open("combined-tv.m3u", "w", encoding="utf-8").write("\n".join(out) + "\n")
print(json.dumps({k: dict(v) for k, v in stats.items()}), dict(by_src))
