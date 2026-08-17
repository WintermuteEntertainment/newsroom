#!/usr/bin/env python3
"""Daily news digest: headlines ranked by breadth of coverage.

Snippet material, 19-outlet panel: for each story, snippets draw on whichever panel
outlets are covering that specific story most closely (most real articles in the
cluster), not a fixed subset -- broadened 2026-07-27 from a 3-outlet restriction
(Reuters/WSJ/NYT) because tone consistency is the pipeline's job (SYS_SNIP), not a
sourcing restriction's. The featured headline/link for a story also comes from
whichever outlet is following it closest, by the same measure.

Correctness checks, each added in response to a specific observed failure:
  1. Event-coherence audit splits clusters that merge same-entity/different-event
     stories (e.g. a share-price move vs. a lawsuit at the same company).
  2. Members excluded by that audit are RE-CLUSTERED as candidate stories in their
     own right, never discarded -- otherwise splitting silently drops a real story.
  3. Google News items are credited to the publisher in their <source> element;
     the aggregator is transport, never counted as an outlet.
  4. Prevalence denominator is a fixed panel so day-over-day figures compare.
  5. Recurring columns/newsletters are filtered out of the blind-spot section.
  6. Every snippet is entailment-checked against the sources it was built from.
"""
import urllib.request, xml.etree.ElementTree as ET, re, html, json, datetime, textwrap, pathlib, time, os
import email.utils
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor

# Publisher feeds serve stale items: WSJ sections have been observed returning
# articles ~18 months old alongside current ones, and one item dated 11 years back.
# Stale items both pollute the digest and inflate cluster prevalence, so every
# article is date-filtered before anything else looks at it. Overridable via
# outlets_config.json's "max_age_hours" (see load_config() below); this is the
# fallback when that key is absent.
DEFAULT_MAX_AGE_HOURS = 36

# --- LLM output-contract compliance -------------------------------------------------
# Every model-calling stage in this pipeline parses a STRICT output shape and, when the
# parse fails, falls back to a default rather than raising. That is deliberate (one bad
# response must not kill a run) but it means a host whose output does not match the
# contract degrades the digest SILENTLY while the run still reports success. The three
# fallbacks and what each does when it fires:
#   coherence_audit  -> keep every member: the cluster is never split, OVERSTATING prevalence.
#   fragment_merge   -> no merge: one event stays fragmented, UNDERSTATING prevalence.
#   entailment_check -> FIXED in 9f12424, previously the one stage that failed OPEN. A reply
#                       with no verdict word now classifies as None (entail_verdict), is
#                       re-asked once, and if still absent publishes as "unverified" rather
#                       than clean. Blank summaries are not sent to the model at all.
#   exclusives_dedup -> treated as not-a-duplicate: a story already covered may re-appear.
# So compliance is recorded per stage and reported with the run. Added 2026-07-28 as the
# precondition for evaluating local models: "the run finished" is not evidence the model
# could be understood.
CONTRACT = defaultdict(lambda: {"calls": 0, "parsed": 0, "failed": 0, "samples": []})


def contract_reset():
    CONTRACT.clear()


def contract_note(stage, ok, text=""):
    """Record one stage response as parseable or not; keep a few failing samples."""
    c = CONTRACT[stage]
    c["calls"] += 1
    if ok:
        c["parsed"] += 1
    else:
        c["failed"] += 1
        if len(c["samples"]) < 3:
            c["samples"].append((text or "")[:200])


# --- tolerant response readers -------------------------------------------------------
# These accept the output shapes a chatty or reasoning-style model actually produces
# without loosening what counts as a valid ANSWER. The previous readers assumed the
# terse output of an instruction-tuned API model:
#   * JSON was located with re.search(r"\{.*\}", re.S) -- greedy, so any prose or
#     <think> block containing a brace made the match span from the first { to the last
#     }, and json.loads failed on the whole thing.
#   * The merge verdict was verdict.startswith("SAME"), so "The clusters describe the
#     SAME event" read as no-merge -- indistinguishable from a genuine DIFFERENT.
# Both failures are silent (see CONTRACT above), so tolerance here is a correctness fix,
# not a convenience: it is the difference between reading a model's answer and ignoring it.
THINK = re.compile(r"<(think|thinking|reasoning|scratchpad)>.*?</\1>", re.S | re.I)


def strip_reasoning(text):
    """Remove <think>...</think> blocks and ```json fences that wrap a real answer.

    An UNCLOSED opening tag is the hard case. Dropping everything after it is right when
    the reply was truncated mid-thought -- there is no answer in there and a parser must
    not half-read the reasoning as one. But some builds emit the closing tag unreliably
    and still finish with a real verdict, and blanking the whole reply threw that verdict
    away: measured 2026-08-10, 34 of 93 fragment_merge failures and 47 of 48
    entailment_check failures logged as empty. So the unclosed branch keeps the LAST
    non-blank line when one survives -- a model that reasons past its closing tag is
    scored on its conclusion, while a genuinely truncated reply still yields nothing
    because its final line is the reasoning itself, which carries no verdict word and is
    refused by the readers downstream.
    """
    t = THINK.sub(" ", text or "")
    m = re.search(r"<(think|thinking|reasoning)>", t, flags=re.I)
    if m:
        tail = [ln for ln in t[m.end():].splitlines() if ln.strip()]
        t = t[:m.start()] + ("\n" + tail[-1] if tail else " ")
    t = re.sub(r"```(?:json|JSON)?\s*(.*?)```", r"\1", t, flags=re.S)
    return t.strip()


def extract_json(text):
    """First brace-balanced JSON object in the text, or None.

    Scans for a { and walks forward tracking depth (string- and escape-aware) to find its
    matching }, so a JSON object embedded in prose parses and a stray brace elsewhere does
    not swallow it. Tries each candidate in turn, returning the first that json.loads.
    """
    t = strip_reasoning(text)
    for start in (i for i, ch in enumerate(t) if ch == "{"):
        depth, instr, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if esc:
                esc = False
                continue
            if ch == "\\" and instr:
                esc = True
                continue
            if ch == '"':
                instr = not instr
                continue
            if instr:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break          # malformed; try the next opening brace
    return None


def read_same_different(text):
    """'SAME' / 'DIFFERENT' / None, read from a bare token or a short sentence.

    Returns None when neither word appears, or when both do with no clear decision -- the
    caller records that as a contract failure instead of defaulting to a verdict. A leading
    bare token wins outright; otherwise a negated form ('not the same') is read as DIFFERENT
    before the bare 'same' in it can be, and last-mentioned wins only as a final resort so a
    model that reasons aloud before answering is scored on its conclusion.
    """
    t = strip_reasoning(text).upper()
    if not t:
        return None
    head = re.match(r"\W*(SAME|DIFFERENT)\b", t)
    if head:
        return head.group(1)
    if re.search(r"\b(NOT THE SAME|NOT SAME|ARE DIFFERENT|IS DIFFERENT)\b", t):
        return "DIFFERENT"
    hits = [(m.start(), m.group(1)) for m in re.finditer(r"\b(SAME|DIFFERENT)\b", t)]
    if hits:
        return hits[-1][1]
    # Glued-prefix recovery. Observed across every run 2026-08-05..08-10: 'ghiSAME',
    # 'ffinDIFFERENT', 'clusteringSAME', 'UniMOREDifferent', 'SbSAME' -- 26 of the 93
    # fragment_merge contract failures on record. The model HAD decided; a stray token
    # fragment landed in front of the answer, \b found no boundary there, and the verdict
    # was discarded -- leaving two fragments of one story listed as separate news.
    #
    # Two limits keep this from inventing verdicts. The word must END cleanly, so
    # 'Differential' (a real word that merely starts with DIFFERENT) is still refused
    # rather than read as a truncated verdict. And a verdict followed by 'EVENT' is
    # refused because the prompt itself ends 'Same event?' -- 'UniSame event?' is a model
    # parroting the question back, and reading that as SAME would merge two unrelated
    # stories on the strength of an echo. Disagreeing glued words stay None as before.
    glued = {m.group(1) for m in re.finditer(r"(SAME|DIFFERENT)(?![A-Z0-9])(?!\s*EVENT)", t)}
    if len(glued) == 1:
        return glued.pop()
    return None


def contract_report():
    """Per-stage {calls, parsed, failed, rate, samples}, sorted by worst rate first."""
    out = {}
    for stage, c in CONTRACT.items():
        out[stage] = {"calls": c["calls"], "parsed": c["parsed"], "failed": c["failed"],
                      "rate": round(c["parsed"] / c["calls"], 4) if c["calls"] else None,
                      "samples": list(c["samples"])}
    return dict(sorted(out.items(), key=lambda kv: (kv[1]["rate"] is None, kv[1]["rate"])))


def parse_pubdate(s):
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def age_hours(item, now=None):
    d = parse_pubdate(item.get("pub", ""))
    if d is None:
        return None
    return (( now or datetime.datetime.now(datetime.timezone.utc)) - d).total_seconds() / 3600

UA = {"User-Agent": "Mozilla/5.0 (compatible; news-digest/1.0)"}
GN = "https://news.google.com/rss/search?q=when:1d+site:{}&hl=en-US&gl=US&ceid=US:en"

# outlets_config.json is edited live via the site's settings panel and gitignored -- it is
# data, not source, the same reasoning as the generated digest CSVs (see D:\Jazz\newsroom
# commit "Stop tracking generated digest CSVs in git"). It layers onto, never replaces, the
# hardcoded DEFAULT_* below: removing then re-adding a default outlet by its exact name
# restores its original (often higher-quality direct-feed) route rather than downgrading it
# to a generic Google News domain lookup.
CONFIG_PATH = pathlib.Path(__file__).resolve().parent / "outlets_config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(CONFIG_PATH)


# All 19 default panel outlets are eligible snippet material, eligible primary/lead outlet,
# and eligible exclusives, as of 2026-07-27 -- there is no longer a 3-outlet "voice" tier.
# PANEL order is used only as a deterministic tie-break (e.g. exclusives round-robin
# fairness); it carries no sourcing privilege.
DEFAULT_PANEL = ["Reuters", "Wall Street Journal", "New York Times", "Associated Press", "Bloomberg",
                 "Financial Times", "Washington Post", "CNBC", "Politico", "BBC", "Guardian", "NPR",
                 "Al Jazeera",
                 # Canadian outlets, vetted with the user 2026-07-26
                 "CBC", "Montreal Gazette", "Globe and Mail", "National Post", "Toronto Star", "CTV News"]

# Direct RSS where the publisher allows it; Google News as transport where it does not
# (reuters.com is behind DataDome; AP/CNBC direct feeds return 403 from this sandbox).
def default_feeds():
    """Direct RSS wherever the publisher serves it; Google News only as TRANSPORT for
    publishers that do not, with every item credited to the originating publisher (see
    fetch_one). Google News is never an outlet in its own right and is never counted in
    prevalence. Routes verified 2026-07-26:

      direct, healthy  : NYT, Washington Post, BBC, Guardian, NPR, Al Jazeera, CNBC, CBC
      transport needed : Reuters   -- 401 on every reuters.com feed path (bot protection)
                         WSJ       -- feeds.a.dj.com AND www.wsj.com frozen at 27 Jan 2025
                         Gazette   -- publishes no RSS; /feed/ returns an HTML page
                         Globe, National Post, Toronto Star, AP, Politico, FT, Bloomberg
    """
    f = []
    for s in ["HomePage", "World", "US", "Business", "Technology", "Politics", "Science", "Health"]:
        f.append(("New York Times", f"https://rss.nytimes.com/services/xml/rss/nyt/{s}.xml"))
    # WaPo sections are individually thin (1-5 items), so several are needed for coverage
    for s in ["world", "national", "business", "politics"]:
        f.append(("Washington Post", f"https://feeds.washingtonpost.com/rss/{s}"))
    for s in ["topstories", "world", "canada", "politics", "business"]:
        f.append(("CBC", f"https://www.cbc.ca/webfeed/rss/rss-{s}"))
    f += [("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
          ("BBC", "https://feeds.bbci.co.uk/news/rss.xml"),
          ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
          ("Guardian", "https://www.theguardian.com/world/rss"),
          ("Guardian", "https://www.theguardian.com/us-news/rss"),
          ("Guardian", "https://www.theguardian.com/business/rss"),
          ("Guardian", "https://www.theguardian.com/world/canada/rss"),
          ("NPR", "https://feeds.npr.org/1001/rss.xml"),
          ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
          ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html")]
    for dom in ["reuters.com", "wsj.com", "montrealgazette.com", "theglobeandmail.com",
                "nationalpost.com", "thestar.com", "ctvnews.ca",
                "apnews.com", "politico.com", "ft.com", "bloomberg.com"]:
        f.append(("_gn", GN.format(dom)))
    return f


# Outlets whose items arrive through their OWN feed, where position in the feed reflects the
# paper's editorial ordering. For Google-transported outlets the order is Google's relevance
# ranking, so it says nothing about how the paper played the story -- feed_rank must not be
# read as editorial prominence for those, and the exclusives section says so.
DIRECT_FEED = {"New York Times", "Washington Post", "CBC", "BBC", "Guardian", "NPR",
               "Al Jazeera", "CNBC"}

DEFAULT_CANON = {"Reuters": "Reuters", "The Wall Street Journal": "Wall Street Journal", "WSJ": "Wall Street Journal",
         "The New York Times": "New York Times", "Associated Press": "Associated Press",
         "AP News": "Associated Press", "The Associated Press": "Associated Press", "CNBC": "CNBC",
         "Politico": "Politico", "POLITICO": "Politico", "Financial Times": "Financial Times",
         "The Washington Post": "Washington Post", "Bloomberg": "Bloomberg", "Bloomberg.com": "Bloomberg",
         "The Guardian": "Guardian", "BBC": "BBC", "BBC News": "BBC", "NPR": "NPR",
         "Al Jazeera": "Al Jazeera", "Al Jazeera English": "Al Jazeera",
         "CBC": "CBC", "CBC.ca": "CBC", "CBC News": "CBC", "CBC.ca | Top Stories News": "CBC",
         "Montreal Gazette": "Montreal Gazette", "montrealgazette.com": "Montreal Gazette",
         "The Globe and Mail": "Globe and Mail", "Globe and Mail": "Globe and Mail",
         "National Post": "National Post", "nationalpost.com": "National Post",
         "Toronto Star": "Toronto Star", "thestar.com": "Toronto Star",
         "The Toronto Star": "Toronto Star", "CTV News": "CTV News", "CTVNews.ca": "CTV News"}

DEFAULT_DOMAIN_CANON = {"ft.com": "Financial Times", "reuters.com": "Reuters", "wsj.com": "Wall Street Journal",
                "nytimes.com": "New York Times", "apnews.com": "Associated Press",
                "washingtonpost.com": "Washington Post", "bloomberg.com": "Bloomberg",
                "cnbc.com": "CNBC", "politico.com": "Politico", "bbc.co": "BBC", "bbc.com": "BBC",
                "theguardian.com": "Guardian", "npr.org": "NPR", "aljazeera.com": "Al Jazeera",
                "cbc.ca": "CBC", "montrealgazette.com": "Montreal Gazette",
                "theglobeandmail.com": "Globe and Mail", "nationalpost.com": "National Post",
                "thestar.com": "Toronto Star", "ctvnews.ca": "CTV News"}

# Domain each Google-News-transported default outlet is filed under -- used to (a) exclude
# its feed row when removed and (b) recognize a re-add-by-name as a restore rather than a
# new outlet. Direct-feed defaults (DIRECT_FEED) need no entry: removing them is a plain
# source-name filter over default_feeds(), and they cannot be "added" with a different
# fetch method since re-adding by name always restores the original direct route.
DEFAULT_DOMAINS = {"Reuters": "reuters.com", "Wall Street Journal": "wsj.com",
                   "Montreal Gazette": "montrealgazette.com", "Globe and Mail": "theglobeandmail.com",
                   "National Post": "nationalpost.com", "Toronto Star": "thestar.com",
                   "CTV News": "ctvnews.ca", "Associated Press": "apnews.com",
                   "Politico": "politico.com", "Financial Times": "ft.com", "Bloomberg": "bloomberg.com"}


def resolve_outlets(cfg: dict) -> dict:
    """Merges outlets_config.json's removed/added outlets onto the hardcoded defaults. Pure
    function of cfg (no module state read or written) so the server's settings-panel
    endpoints can call this fresh on every request instead of relying on this module's own
    globals, which are computed once at import time and would otherwise go stale the moment
    a save happens without a server restart."""
    removed = set(cfg.get("removed_outlets") or [])
    raw_added = [a for a in (cfg.get("added_outlets") or []) if a.get("name") and a.get("domain")]
    restored = {a["name"] for a in raw_added if a["name"] in DEFAULT_PANEL}
    added = [a for a in raw_added if a["name"] not in DEFAULT_PANEL]
    removed -= restored

    panel, seen = [], set()
    for name in [n for n in DEFAULT_PANEL if n not in removed] + [a["name"] for a in added]:
        if name not in seen:
            seen.add(name)
            panel.append(name)

    canon, domain_canon = dict(DEFAULT_CANON), dict(DEFAULT_DOMAIN_CANON)
    for a in added:
        canon[a["name"]] = a["name"]
        domain_canon[a["domain"].lower().strip().lstrip(".")] = a["name"]

    excluded_domains = {DEFAULT_DOMAINS[n] for n in removed if n in DEFAULT_DOMAINS}
    feeds = [(src, url) for src, url in default_feeds()
             if src not in removed and not (src == "_gn" and any(d in url for d in excluded_domains))]
    feeds += [("_gn", GN.format(a["domain"])) for a in added]

    active = [{"name": n, "source": "default", "fetch": "direct" if n in DIRECT_FEED else "google_news",
               "domain": DEFAULT_DOMAINS.get(n)} for n in DEFAULT_PANEL if n not in removed]
    active += [{"name": a["name"], "source": "added", "fetch": "google_news", "domain": a["domain"]}
               for a in added]

    return {"panel": panel, "pset": set(panel), "canon": canon, "domain_canon": domain_canon,
            "feeds": feeds, "active_outlets": active, "removed_defaults": sorted(removed & set(DEFAULT_PANEL))}


def validate_config(cfg: dict) -> str | None:
    """Returns an error message if cfg is not safe to save, else None. Called by the
    server before writing -- resolve_outlets() itself has no opinion on whether its result
    is sane, since a pipeline run invoked directly could legitimately want an unusual panel."""
    removed, added = cfg.get("removed_outlets") or [], cfg.get("added_outlets") or []
    if not isinstance(removed, list) or not all(isinstance(x, str) for x in removed):
        return "removed_outlets must be a list of outlet names."
    if not isinstance(added, list):
        return "added_outlets must be a list."
    taken_domains = set(DEFAULT_DOMAIN_CANON)
    for a in added:
        if not isinstance(a, dict) or not a.get("name") or not a.get("domain"):
            return "Each added outlet needs a name and a domain."
        domain = str(a["domain"]).lower().strip().lstrip(".")
        if a["name"] not in DEFAULT_PANEL and domain in taken_domains:
            return f"The domain '{domain}' is already in use by another outlet."
        taken_domains.add(domain)
    if len(resolve_outlets(cfg)["panel"]) < 3:
        return "The panel needs at least 3 outlets left for prevalence ranking to mean anything."
    for key, (lo, hi) in {"top_n": (1, 100), "max_age_hours": (1, 168)}.items():
        if cfg.get(key) is not None:
            try:
                v = float(cfg[key])
            except (TypeError, ValueError):
                return f"{key} must be a number."
            if not (lo <= v <= hi):
                return f"{key} must be between {lo} and {hi}."
    return None


_cfg = load_config()
_resolved = resolve_outlets(_cfg)
PANEL = _resolved["panel"]
PSET = _resolved["pset"]
CANON = _resolved["canon"]
DOMAIN_CANON = _resolved["domain_canon"]
MAX_AGE_HOURS = _cfg.get("max_age_hours") or DEFAULT_MAX_AGE_HOURS


def build_feeds():
    return _resolved["feeds"]


def strip_tags(t):
    return html.unescape(re.sub(r"<[^>]+>", " ", t or "")).replace("&nbsp;", " ").strip()


def fetch_one(spec, attempts=3):
    """Retries transient failures. Observed 2026-07-26: a run lost Washington Post, FT and
    Bloomberg entirely to one-off 'Remote end closed connection' / read-timeout errors. That
    is not cosmetic -- prevalence is a count of panel outlets, so a dropped feed silently
    understates every story those outlets carried. Retried with backoff; a feed that still
    fails is reported in degraded_sources and disclosed in the digest header."""
    src, url = spec
    root = None
    for k in range(attempts):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read()
            root = ET.fromstring(raw)
            break
        except Exception as e:
            err = str(e)[:100]
            if k == attempts - 1:
                return {"url": url, "error": err, "items": []}
            time.sleep(1.5 * (k + 1))
    is_gn = "news.google.com" in url
    out = []
    for it in root.iter():
        if not it.tag.endswith("item"):
            continue
        d, pub = {}, None
        for ch in it:
            tag = ch.tag.split("}")[-1]
            if tag == "source":
                pub = strip_tags(ch.text) or None
            elif tag in ("title", "description", "link", "pubDate"):
                d.setdefault(tag, ch.text or "")
        title = strip_tags(d.get("title"))
        if is_gn:
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)   # trailing " - Publisher"
        if len(title) < 15:
            continue
        # Transported items are credited to the <source> publisher. Google sometimes reports it
        # as a bare host ('ep.ft.com') or a section brand ('FT Board Director Programme'); map
        # those onto the real outlet by domain rather than admitting them as pseudo-outlets.
        if is_gn and pub and pub not in CANON:
            host_hint = (pub if "." in pub else "") or (d.get("link") or "")
            for dom, name in DOMAIN_CANON.items():
                if dom in host_hint.lower() or dom in pub.lower():
                    pub = name
                    break
        outlet = CANON.get(pub, pub) if is_gn else src
        out.append({"source": outlet or "Unattributed", "title": title,
                    "summary": strip_tags(d.get("description"))[:600],
                    "link": (d.get("link") or "").strip(), "pub": (d.get("pubDate") or "").strip(),
                    # position within its feed: a proxy for the outlet's own play of the story
                    "feed_rank": len(out), "feed": url.rsplit("/", 1)[-1][:40]})
    return {"url": url, "error": None, "items": out}


def fetch_all(max_age_hours=MAX_AGE_HOURS):
    feeds = build_feeds()
    with ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(fetch_one, feeds))
    items = [i for r in res for i in r["items"]]
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh, stale, undated = [], [], []
    for i in items:
        a = age_hours(i, now)
        i["age_hours"] = None if a is None else round(a, 1)
        if a is None:
            undated.append(i)          # no usable date -> cannot vouch for it, exclude
        elif a <= max_age_hours:
            fresh.append(i)
        else:
            stale.append(i)
    seen, uniq = set(), []
    for i in sorted(fresh, key=lambda x: x["feed_rank"]):
        k = (i["source"], re.sub(r"\W+", "", i["title"].lower())[:90])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(i)
    kept = Counter(i["source"] for i in uniq)
    meta = {"errors": [(r["url"], r["error"]) for r in res if r["error"]],
            "n_raw": len(items), "n_stale": len(stale), "n_undated": len(undated),
            "stale_by_source": dict(Counter(i["source"] for i in stale)),
            "kept_by_source": {p: kept.get(p, 0) for p in PANEL},
            "oldest_kept_hours": max([i["age_hours"] for i in uniq], default=None)}
    # A voice source contributing almost nothing means its feed route is broken, not that
    # it had a quiet day -- surface it rather than silently publishing a digest without it.
    meta["degraded_sources"] = [p for p in PANEL if kept.get(p, 0) < 5]
    return uniq, meta


def cluster(items, threshold=0.80):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import AgglomerativeClustering
    if len(items) < 2:
        return [items] if items else []
    texts = [f'{i["title"]}. {i["summary"][:220]}' for i in items]
    X = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1,
                        sublinear_tf=True, max_features=60000).fit_transform(texts).toarray()
    labels = AgglomerativeClustering(n_clusters=None, distance_threshold=threshold,
                                     metric="cosine", linkage="average").fit_predict(X)
    g = defaultdict(list)
    for lab, it in zip(labels, items):
        g[lab].append(it)
    return list(g.values())


NON_ARTICLE = re.compile(
    r"^\s*(podcast|watch|listen|video|live|photos?|opinion|editorial|review|recap|newsletter|"
    r"morning briefing|evening briefing|the daily|briefing|quiz|crossword|wordle|horoscope|"
    r"what to watch|best of|slideshow|in pictures|analysis)\b[\s:\u2014-]", re.I)

# Live-blog and index stubs: contentless headlines that carry no event and cannot be
# summarised ("Here's the latest.", "What we know so far", "Live updates").
# Soft features and obituaries read as exclusives (single outlet, played high) but are not the
# scoops this section exists to surface. Applied only to the exclusives shortlist, never to the
# prevalence ranking, where a widely-carried human-interest story is legitimately news.
SOFT_FEATURE = re.compile(
    r"\b(dies at \d+|dead at \d+|obituary|is obsessed with|are obsessed with|"
    r"what to cook|recipe|wirecutter|best \w+ of|gift guide|style|fashion week|"
    r"crossword|puzzle|horoscope|travel guide|things to do|review:|"
    r"could hold the secrets?|secrets? to a longer|isn[\u2019']t \w+\. it[\u2019']s|"
    r"see the \$[\d.]+ ?(?:million|billion)? world of|what it[\u2019']s like to)\b", re.I)

# Opinion/editorial content, which the user asked to keep out: reported news only. Matched on
# the outlet's own prefixes ("WSJ Opinion:", "Opinion |") rather than on subject matter.
OPINION = re.compile(r"^\s*(?:wsj\s+)?(?:opinion|editorial|commentary|column|letters?|"
                     r"review\s*&\s*outlook|the\s+editorial\s+board)\s*[:|\u2014-]|"
                     r"\bopinion\s*[:|]\s*|"
                     # book/film reviews arrive as "'Title' Review: Subtitle" -- the quoted
                     # title defeats a leading anchor, so match the mid-title form too
                     r"[\u2019'\"\u201d]\s+review\s*:", re.I)

STUB = re.compile(r"^\s*(here'?s? (?:the latest|what)|what (?:we know|to know)|live updates?|"
                  r"the latest|latest updates?|key takeaways|as it happened|catch up|"
                  r"follow live|updates?)\b", re.I)


def is_non_article(title):
    """Podcasts, live blogs and puzzle pages cluster like news but are not stories.
    Caught at title level: the cluster-coherence audit only sees whole groups, so a
    podcast episode that clusters with real coverage of the same topic slips past it."""
    t = (title or "").strip()
    return bool(NON_ARTICLE.match(t)) or bool(STUB.match(t)) or bool(OPINION.search(t)) or \
        bool(re.match(r"^[A-Z][A-Z\s']{6,}:", t)) or len(t.split()) < 4


def as_story(members):
    panel = {m["source"] for m in members} & PSET
    return {"members": members, "outlets": sorted(panel), "n_outlets": len(panel),
            "n_items": len(members), "extra": sorted({m["source"] for m in members} - PSET)}


def real_panel_members(s):
    """Panel-outlet members that are actual articles (not podcasts/live blogs) -- the
    material eligible to be featured as primary() or fed to the model as snippet source.
    Replaces the old real_voice_members(), which restricted this to Reuters/WSJ/NYT; any
    of the 19 panel outlets is now eligible, broadened 2026-07-27."""
    return [m for m in s["members"] if m["source"] in PSET and not is_non_article(m["title"])]


def coverage_leaders(s, max_outlets=4):
    """Panel outlets covering this story, ranked by depth of REAL-article coverage (most
    real articles in the cluster first, PANEL position as tie-break) -- this is "whichever
    outlet is following the story closest", replacing the old fixed Reuters/WSJ/NYT voice
    tier. An outlet with only non-article members (podcast/live-blog) for this story
    contributes no material and is excluded here even though it still counts toward
    n_outlets/ALSO CARRIED BY."""
    by_outlet = defaultdict(list)
    for m in real_panel_members(s):
        by_outlet[m["source"]].append(m)
    ranked = sorted(by_outlet, key=lambda src: (-len(by_outlet[src]), PANEL.index(src)))
    return ranked[:max_outlets], by_outlet


def primary(s):
    """Headline shown for a story: the first real article from whichever panel outlet has
    the deepest real-article coverage of it, then any real article, then anything. Top-ranked
    rows are pre-filtered (real_panel_members) to guarantee the first case, so a main row's
    headline and link always come from an outlet that actually covered it as an article --
    without that filter a story whose only leading member was a podcast fell through to a
    non-covering headline."""
    leaders, by_outlet = coverage_leaders(s, max_outlets=1)
    if leaders:
        return by_outlet[leaders[0]][0]
    for pool in ([m for m in s["members"] if not is_non_article(m["title"])], s["members"]):
        if pool:
            return pool[0]
    return s["members"][0]


def voice_payload(s):
    """Source material handed to the model for snippet writing: real articles from the
    outlets covering this story most closely (coverage_leaders), capped per outlet so one
    prolific outlet can't crowd out the others. Falls back to any panel member if no outlet
    has real-article coverage (should not occur for stories reaching write_snippets, which
    are pre-filtered by real_panel_members)."""
    leaders, by_outlet = coverage_leaders(s, max_outlets=4)
    real = [m for src in leaders for m in by_outlet[src][:2]]
    if not real:
        real = sorted([m for m in s["members"] if m["source"] in PSET],
                       key=lambda m: PANEL.index(m["source"]))
    return "\n".join(f'- [{m["source"]}] {m["title"]}\n  {m["summary"][:340]}' for m in real)


def outlet_links(s):
    """One representative article URL per panel outlet covering this story -- the data the
    'also carried by' section links through to (per-outlet buttons on the site, added
    2026-07-27). Prefers that outlet's first REAL article (not podcast/live-blog) so a link
    always lands on an actual story; falls back to any member from that outlet if it has no
    real article in this cluster. Does not invent new links -- only reuses ones already
    fetched into s["members"]."""
    real_by_outlet = defaultdict(list)
    for m in real_panel_members(s):
        real_by_outlet[m["source"]].append(m)
    any_by_outlet = defaultdict(list)
    for m in s["members"]:
        if m["source"] in PSET:
            any_by_outlet[m["source"]].append(m)
    return {o: (real_by_outlet[o][0] if real_by_outlet[o] else any_by_outlet[o][0])["link"]
            for o in s["outlets"]}


def numbered(members):
    return "\n".join(f'{k}. [{m["source"]}] {m["title"]} :: {m["summary"][:160]}'
                     for k, m in enumerate(members))


SYS_COHERE = ("You audit news-story clusters for event coherence. Two articles belong to the same cluster "
              "only if they report the SAME underlying news event, not merely the same company, person, or "
              "topic. A share-price move on trial results and a lawsuit over advertising are DIFFERENT "
              "events even for the same firm.")
SYS_MERGE = ("You judge whether two news-story clusters cover the SAME underlying event. Same event means "
             "the same incident, decision, or development -- follow-up and reaction coverage of one incident "
             "IS the same event ('van rams crowd' and 'police hunt suspect after van ramming' are the same "
             "event; a party's nominee reset and the specific replacement pick are the same event). Merely "
             "sharing a region, actor, or theme is NOT ('two separate incidents in the same sea', 'two "
             "different wildfires in different countries'). Answer SAME or DIFFERENT and nothing else.")
# Strengthened 2026-07-27: source material now comes from whichever of the 19 panel outlets
# covers a story most closely, not a pre-vetted 3-outlet wire/paper-of-record tier, so the
# input itself may carry editorializing, loaded framing, or partisan word choice that a wire
# service would not use. The instruction now names that explicitly rather than assuming
# neutral input.
SYS_SNIP = ("You write neutral, matter-of-fact wire-style news briefs from source material that may itself "
            "be editorialized, opinionated, or use loaded framing -- your job is to normalize it to neutral "
            "reporting, not to reproduce its tone. Report only what happened, in plain declarative sentences, "
            "regardless of how the source characterizes it: drop adjectives, adverbs, and framing that editorialize "
            "(e.g. 'slammed', 'blasted', 'stunning', 'controversial', 'according to critics') and keep only the "
            "underlying facts they attach to. Use ONLY the facts in the supplied headlines and summaries. "
            "Never add facts, figures, superlatives, characterizations, or context absent from the input -- "
            "if the input says 'first test flight since IPO', do not write 'most-watched test since the IPO'. "
            "Do not name news organizations in the prose. If a detail is not in the input, omit it. "
            "No opinion, no speculation, and no adoption of any source's point of view.")
# The check is ONE-DIRECTIONAL: does the summary ASSERT anything the sources do not? A summary
# that merely leaves detail out is doing its job. Without saying so explicitly the model drifts
# into scoring COVERAGE, and the drift is silent because an omission complaint is still shaped
# like a verdict -- it parses, so the contract counters read 100% while the row publishes with a
# red 'unverified' flag. Measured on the 2026-08-04 digest: 20 of 24 rows came back UNSUPPORTED,
# and 12 of those 20 were "the summary does not mention X" -- omissions, not added claims. A 50%
# false-alarm rate on the published digest. Penalising omission also contradicted SYS_SNIP, which
# is explicitly told "If a detail is not in the input, omit it".
SYS_ENTAIL = ("You check whether a summary ADDS information not present in its source material. "
              "This is a one-directional check. A summary is allowed to leave things out: it is "
              "a summary, and omitting a detail is never a problem. Judge ONLY what the summary "
              "asserts. "
              "Reply SUPPORTED if every claim the summary makes appears in the sources -- even if "
              "the summary covers only a small part of them, and even if it omits details you "
              "consider important. "
              "Reply UNSUPPORTED: <the added claim> ONLY when the summary states something the "
              "sources do not support. Never reply UNSUPPORTED because the summary is short, "
              "incomplete, or missing a detail.")


def entail_verdict(text: str) -> str | None:
    """Classify an entailment reply as 'supported' / 'unsupported' / None (no verdict).

    UNSUPPORTED is tested FIRST because the string "UNSUPPORTED" contains "SUPPORTED":
    testing for SUPPORTED first reads every rejection as an approval. Returning None for
    an unrecognised reply is the point of this function -- the old code tested
    `"UNSUPPORTED" in v` directly, so a refusal or an empty response fell through as clean
    and the unverified snippet published as verified.
    """
    up = (text or "").strip().upper()
    if not up:
        return None
    if "UNSUPPORTED" in up:
        return "unsupported"
    if "SUPPORTED" in up:
        return "supported"
    return None


def merge_fragments(host, stories, model, top_n=40, sim_floor=0.12):
    """Merge clusters that split the SAME event, which otherwise understates prevalence.

    TF-IDF clustering at the threshold needed to keep distinct events apart also splits one
    event across differently-worded coverage: a ramming attack arrived as three clusters
    (5 + 3 + 2 outlets) where the true figure was 8, so it ranked 4th instead of 1st.
    Lowering the clustering threshold is not the fix -- it merges genuinely different events
    (two separate incidents in the same sea scored 0.304, higher than two true same-event
    pairs). So similarity only SHORTLISTS pairs and the model adjudicates, as with coherence.
    Bounded to the top_n ranked stories: fragmentation only distorts the visible ranking, and
    this keeps the pass to a few dozen calls."""
    if len(stories) < 2:
        return stories, 0
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    head, tail = stories[:top_n], stories[top_n:]
    txt = [" ".join(m["title"] for m in s["members"][:4]) for s in head]
    S = cosine_similarity(TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                                          sublinear_tf=True).fit_transform(txt))
    np.fill_diagonal(S, 0)
    pairs = sorted(((float(S[a, b]), a, b) for a in range(len(head))
                    for b in range(a + 1, len(head)) if S[a, b] >= sim_floor), reverse=True)
    if not pairs:
        return stories, 0
    def desc(s):
        return "\n".join(f'- [{m["source"]}] {m["title"]}' for m in s["members"][:4])
    res = host.llm([{"prompt": f"CLUSTER A:\n{desc(head[a])}\n\nCLUSTER B:\n{desc(head[b])}\n\nSame event?",
                     "system": SYS_MERGE, "max_tokens": 8, "model": model} for _, a, b in pairs])
    # union-find over confirmed pairs, so A~B and B~C collapse into one story
    parent = list(range(len(head)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    n_merged = 0
    for (_, a, b), r in zip(pairs, res):
        txt = (r.get("text") or "") if isinstance(r, dict) else ""
        verdict = read_same_different(txt)
        contract_note("fragment_merge", verdict is not None, txt)
        if verdict == "SAME":
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
                n_merged += 1
    if not n_merged:
        return stories, 0
    bucket = defaultdict(list)
    for k in range(len(head)):
        bucket[find(k)].append(k)
    out = []
    for root, ks in bucket.items():
        seen, members = set(), []
        for k in ks:                       # keep the highest-ranked cluster's members first,
            for m in head[k]["members"]:   # so primary() still picks a lead article
                key = (m["source"], m["title"])
                if key not in seen:
                    seen.add(key)
                    members.append(m)
        out.append(as_story(members))
    out += tail
    out.sort(key=lambda s: (s["n_outlets"], s["n_items"]), reverse=True)
    return out, n_merged


def audit_coherence(host, groups, model, rounds=2):
    """Split multi-event clusters. Members excluded by a split are re-clustered and
    re-audited rather than discarded, so splitting never loses a story."""
    stories, pool = [], [g for g in groups if len({m["source"] for m in g} & PSET) >= 2]
    for _ in range(rounds):
        if not pool:
            break
        reqs = [{"prompt": "Below are numbered articles grouped as one story. Identify the single largest "
                           "subset that reports the SAME event.\nAnchor on whichever event has the most "
                           "articles. Exclude any article about a different event.\nAlso flag if the group is "
                           "a recurring column/newsletter/programme rather than a news event.\n"
                           'Reply ONLY with JSON: {"keep":[indices],"event":"short label",'
                           '"is_column":true/false}\n\n' + numbered(g),
                 "system": SYS_COHERE, "model": model, "max_tokens": 300} for g in pool]
        res = host.llm(reqs, max_concurrency=6)
        orphans = []
        for g, r in zip(pool, res):
            txt = (r.get("text") or "") if isinstance(r, dict) else ""
            keep, label, iscol, parsed = list(range(len(g))), None, False, False
            j = extract_json(txt)
            if isinstance(j, dict) and "keep" in j:
                k = [i for i in j.get("keep", []) if isinstance(i, int) and 0 <= i < len(g)]
                if k:
                    keep = k
                label, iscol = j.get("event"), bool(j.get("is_column"))
                parsed = True
            # An unparseable response leaves keep=all: the cluster is NOT split, which
            # overstates prevalence. Record it rather than letting it pass unseen.
            contract_note("coherence_audit", parsed, txt)
            if iscol:
                continue
            ks = set(keep)
            orphans += [g[i] for i in range(len(g)) if i not in ks]
            st = as_story([g[i] for i in keep])
            if st["n_outlets"] >= 2:
                st["event"] = label
                stories.append(st)
        # rescued members get a fresh clustering pass; survivors re-enter the audit
        pool = [c for c in cluster(orphans) if len({m["source"] for m in c} & PSET) >= 2] if orphans else []
    for c in pool:                      # anything still pending after the last round
        st = as_story(c)
        if st["n_outlets"] >= 2:
            st["event"] = None
            stories.append(st)
    # A story made up ENTIRELY of non-article items (all-podcast, all-live-blog) is not news
    stories = [s for s in stories if any(not is_non_article(m["title"]) for m in s["members"])]
    stories.sort(key=lambda s: (s["n_outlets"], s["n_items"]), reverse=True)
    return stories


def snippet_flags(t):
    """Detect malformed snippets. The entailment check cannot see these: a snippet cut off
    mid-clause ('...according to') adds no unsupported claim, so it passes entailment while
    being unpublishable. Truncation was observed when the no-outlet-names rule collided with
    an attribution clause the model had already started."""
    ts = (t or "").rstrip()
    p = []
    if not ts:
        return ["empty"]
    if not ts.endswith((".", "!", "?", '"', "\u201d", ")")):
        p.append("no_terminal_punctuation")
    if re.search(r"\b(according to|said|told|reported by|per|citing)\s*$", ts, re.I):
        p.append("dangling_attribution")
    # Meta-commentary: the model narrating its own limits instead of writing the brief
    # ("I don't have enough information...", "Here is a one-sentence snippet based on..."). This
    # passes entailment -- it asserts nothing about the world -- but is unpublishable, and it
    # reached the saved digest at rank 17 on 2026-07-26.
    # Two conditions must BOTH hold, because first-person negation and the words "not enough
    # information" are ordinary wire prose: 'The prime minister said "we cannot allow this to
    # continue"', 'Prosecutors said there was not enough information to charge the driver.'
    # (1) the first person must be the SENTENCE SUBJECT -- anchored at the start, so quoted or
    # reported speech ('... said we cannot ...') never matches; and (2) the sentence must refer
    # to the writing task itself, not to any event in the world.
    if (re.match(r"\s*(?:I|we)\s+(?:don'?t|do not|cannot|can'?t|couldn'?t|am unable|"
                 r"are unable|have insufficient|lack)\b", ts, re.I)
            and re.search(r"\b(?:snippet|summary|summari[sz]e|brief|blurb|"
                          r"(?:information|content|detail|context|coverage)\s+"
                          r"(?:provided|given|available|supplied|here)|"
                          r"provided (?:coverage|content|information)|write (?:this|a)\b)",
                          ts, re.I)) or \
       re.search(r"\b(?:here|below)\s+is\s+(?:a|the)\s+(?:one|two|three)?-?\s*sentence\s+"
                 r"(?:snippet|summary|brief)\b|"
                 r"\bthe only (?:content|information) provided\b|"
                 r"\bas an AI\b|\bbased strictly on what is supported\b|"
                 r"\bno additional details?, context, or supporting facts\b", ts, re.I):
        p.append("meta_commentary")
    # The digest already names its sources in the COVERED CLOSEST BY line; naming them in the
    # prose too is both redundant and the failure mode that produced 'according to <paper> and <paper>'.
    if re.search(r"\b(Wall Street Journal|New York Times|Reuters|Associated Press|Bloomberg|"
                 r"Financial Times|Washington Post|CNBC|Politico|BBC|Guardian|NPR|Al Jazeera|"
                 r"Google News)\b", ts):
        p.append("names_outlet")
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", ts) if len(x.split()) > 3]
    if len(sentences) < 2:
        p.append("single_sentence")
    if len(ts) < 120:
        p.append("too_short")
    return p


OUTLET_RX = (r"(?:the\s+)?(?:Wall Street Journal|New York Times|Reuters|Associated Press|Bloomberg|"
             r"Financial Times|Washington Post|CNBC|Politico|BBC(?: News)?|Guardian|NPR|Al Jazeera|"
             r"Google News)")


def _mangled(s):
    """Does this sentence read as wreckage left by deleting a clause?

    Deliberately narrow. An earlier version flagged any ', which <verb>' clause, which
    false-positives on ordinary prose ('the plant, which employs 400 workers, will close')
    and cost real sentences. The orphaned-relative-clause case it was meant to catch is now
    handled at the source: _scrub_sentence removes a trailing 'which/who' clause together
    with the attribution it modifies, so no orphan is produced. What remains detectable
    without guessing is a sentence with no subject -- opening on a relative pronoun or
    conjunction, or on a stranded reporting verb."""
    s = (s or "").strip()
    return bool(re.search(r"^(?:similarly|also)?\s*(?:reported|said|reports|says)\b", s, re.I)) or \
        bool(re.search(r"^(?:which|who|and|but|that)\b", s, re.I)) or \
        bool(re.search(r"^,", s))


def _scrub_sentence(s):
    """Strip attribution from ONE sentence. Where the outlet name carries a trailing relative
    clause ('according to X, which linked ...') the clause is removed with it -- it modifies the
    outlet, not the news, so deleting only the name orphans it."""
    # "..., according to X, which/who <clause>" -> drop attribution AND the clause it governs
    s = re.sub(r"\s*,?\s*(?:according to|per|as reported by)(?:\s+" + OUTLET_RX +
               r")(?:\s*(?:,|and|&)\s*" + OUTLET_RX + r")*\s*,\s*(?:which|who)\b[^.!?]*", "", s, flags=re.I)
    # ", according to coverage from X and Y." -> "."   (also 'per', 'as reported by')
    s = re.sub(r"\s*,?\s*(?:according to|per|as reported by|as reported in|based on reporting (?:by|from))"
               r"(?:\s+(?:coverage|reporting|reports?|an? (?:report|article))\s+(?:from|by|in))?"
               r"(?:\s+" + OUTLET_RX + r")(?:\s*(?:,|and|&)\s*" + OUTLET_RX + r")*\s*", " ", s, flags=re.I)
    # leading "X reports that ..." / "X similarly reported ..." ('that' is often absent)
    s = re.sub(r"^\s*" + OUTLET_RX + r"\s+(?:similarly\s+|also\s+)?"
               r"(?:reports?|reported|says?|said|notes?|noted|adds?|added|found|writes?|wrote)"
               r"\s+(?:that\s+)?", "", s, flags=re.I)
    s = re.sub(r"\s*\(\s*" + OUTLET_RX + r"\s*\)", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"\s+([.,;:])", r"\1", s)
    s = re.sub(r"\s*,\s*\.", ".", s)
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if s and not s.endswith((".", "!", "?", '"', "\u201d", ")")):
        s += "."
    return s


def scrub_attribution(t):
    """Remove source-attribution clauses mechanically. Prompting alone does not hold: the model
    reliably reintroduces 'according to <paper>' even when told not to, because the phrasing is
    idiomatic in wire copy. The digest names its sources in the COVERED CLOSEST BY line instead.

    Works sentence by sentence, and only ever discards a sentence THIS function damaged:
    a sentence the scrub left untouched is passed through verbatim, however it reads. Judging
    prose the scrub did not write is not this function's job -- pre-existing malformation is
    reported by snippet_flags and handled by the regeneration pass, which can consult the
    sources. Dropping such sentences here silently loses reported content."""
    orig = (t or "").strip()
    if not orig:
        return orig
    kept = []
    for s in [x for x in re.split(r"(?<=[.!?])\s+", orig) if x.strip()]:
        c = _scrub_sentence(s)
        if c == s:
            kept.append(s)                      # untouched: never second-guess it
            continue
        if not c or len(c.split()) < 4 or _mangled(c):
            continue                            # our own edit broke it: drop just this sentence
        kept.append(c)
    return " ".join(kept) if kept else orig


META_LEAD = re.compile(
    r"^.*?\b(?:here is|here'?s)\s+(?:a|the)\s+(?:one|two|three)?-?\s*sentence\s+"
    r"(?:snippet|summary|brief)[^:.]*[:.]\s*", re.I | re.S)


def strip_meta(t):
    """Salvage a snippet the model prefaced with commentary about its own limits.

    Observed at rank 17 on 2026-07-26: "I don't have enough information to write this snippet.
    The only content provided is ... Here is a one-sentence snippet based strictly on what is
    supported: Iran says a Ukrainian attack ... killed a sailor." The usable brief is present;
    only the preamble is not. Prefer cutting to the explicit hand-off phrase, else drop the
    sentences that are commentary and keep the ones that report. Returns "" if nothing reports;
    write_snippets calls this BEFORE its repair pass so an emptied snippet gets regenerated
    (and has a marked fallback if regeneration also fails)."""
    ts = (t or "").strip()
    if not ts:
        return ts
    m = META_LEAD.search(ts)
    if m and ts[m.end():].strip():
        ts = ts[m.end():].strip()
    keep = [x for x in re.split(r"(?<=[.!?])\s+", ts)
            if x.strip() and "meta_commentary" not in snippet_flags(x)]
    out = " ".join(keep).strip()
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def write_snippets(host, stories, model):
    reqs = [{"prompt": "Write a plain-text news snippet of 2-3 sentences summarizing this single story.\n"
                       "Every statement must be directly supported by the coverage below. Output ONLY the "
                       f"snippet prose.\n\nCOVERAGE:\n{voice_payload(s)}",
             "system": SYS_SNIP, "model": model, "max_tokens": 320} for s in stories]
    _sres = host.llm(reqs, max_concurrency=6)
    snips = []
    for r in _sres:
        t = strip_reasoning((r.get("text") or "") if isinstance(r, dict) else "")
        # A snippet stage "fails" by returning nothing usable; the fallback is a placeholder
        # line, so an unusable host shows up as placeholder prose rather than an error.
        contract_note("snippet_writing", bool(t.strip()), t)
        snips.append(t)

    # strip_meta runs BEFORE the repair pass, not after: it can empty a snippet that was pure
    # commentary, and the repair pass is the only thing that can rebuild one. Running it after
    # (as it was until 2026-07-26) meant an emptied snippet published as a blank body.
    snips = [strip_meta(t) for t in snips]

    # Repair pass: regenerate malformed snippets with an explicit completeness instruction.
    retry = [k for k, t in enumerate(snips) if snippet_flags(t)]
    if retry:
        rreqs = [{"prompt": "Write a complete plain-text news snippet summarizing this single story.\n"
                            "Write as many complete sentences as the coverage supports (1-3). Every statement "
                            "must be directly supported by the coverage. Do not name news organizations and do "
                            "not use attribution phrases like 'according to'. End with a full stop. Never stop "
                            "mid-sentence. Output ONLY the snippet prose.\n\n"
                            f"COVERAGE:\n{voice_payload(stories[k])}",
                  "system": SYS_SNIP, "model": model, "max_tokens": 400} for k in retry]
        for k, r in zip(retry, host.llm(rreqs, max_concurrency=6)):
            cand = strip_meta((r.get("text") or "").strip() if isinstance(r, dict) else "")
            # Any non-empty replacement beats an empty snippet outright: comparing flag COUNTS
            # would reject a usable 1-sentence retry (2 flags) in favour of "" (1 flag, "empty").
            if cand and (not snips[k].strip()
                         or len(snippet_flags(cand)) < len(snippet_flags(snips[k]))):
                snips[k] = cand

    snips = [scrub_attribution(t) for t in snips]

    # Last resort: a snippet that is still empty after the repair pass would render as a blank
    # body under its rank header. Fall back to the headline, marked so it is not read as prose
    # the pipeline wrote from the coverage.
    for k, t in enumerate(snips):
        if not t.strip():
            snips[k] = "[No usable summary could be written from the available coverage.]"

    def entail(texts, only=None):
        """Verdicts for `texts`. With `only`, re-check just those indices.

        The re-check passes exist to confirm a rewrite worked, and a rewrite touches only the
        handful of snippets that failed. Re-asking about all ~100 every pass cost 3x100 calls
        to verify ~6 rewrites -- measured on the 2026-07-28 run: 100 calls found 6 failures,
        then 100 more found 5, then 100 more found 3. Indices not in `only` keep their prior
        verdict, which is exactly what a full re-ask returned for them anyway.
        """
        idx = list(range(len(texts))) if only is None else list(only)
        # A blank summary has nothing to verify. Asking anyway wasted a call and returned
        # "UNSUPPORTED: no summary was provided", which is a true statement about the input
        # but not a fact-check -- 5 of the 12 UNSUPPORTED verdicts in the 2026-07-28 baseline
        # corpus were this, contaminating any recall measurement taken from it.
        res = {}
        ask = [i for i in idx if (texts[i] or "").strip()]
        for i in idx:
            if i not in ask:
                res[i] = ""
                contract_note("entailment_check", True, "<skipped: empty summary>")

        def ask_batch(targets):
            ereq = [{"prompt": f"SOURCES:\n{voice_payload(stories[i])}\n\nSUMMARY:\n{texts[i]}\n\nVerdict?",
                     "system": SYS_ENTAIL, "model": model, "max_tokens": 90}
                    for i in targets]
            got = {}
            for i, r in zip(targets, host.llm(ereq, max_concurrency=6)):
                got[i] = strip_reasoning((r.get("text") or "") if isinstance(r, dict) else "")
            return got

        res.update(ask_batch(ask))
        # One retry for replies that carried no verdict at all -- these are usually transient
        # (refusal, truncation, empty). Retrying is cheap and turns most of them into a real
        # verdict; whatever still has none is reported as unverified, never as clean.
        retry = [i for i in ask if entail_verdict(res[i]) is None]
        if retry:
            res.update(ask_batch(retry))
        for i in ask:
            contract_note("entailment_check", entail_verdict(res[i]) is not None, res[i])
        if only is None:
            return [res[i] for i in idx]
        return {i: res[i] for i in idx}

    ver = entail(snips)

    # An UNSUPPORTED verdict must have a consequence. Recording it in a column while still
    # publishing the unsupported claim is not a check -- so the snippet is rewritten with the
    # specific added claim quoted back, then re-checked. If it still fails, the sentence
    # carrying the claim is dropped rather than published.
    # Anything that is not a positive verdict needs a consequence: an explicit UNSUPPORTED,
    # and equally a reply with NO verdict (None). The old test was `"UNSUPPORTED" in v`, which
    # let a refusal or empty reply publish as verified. Empty snippets are excluded -- they
    # were never asked and have nothing to repair.
    bad = [k for k, v in enumerate(ver)
           if (snips[k] or "").strip() and entail_verdict(v) != "supported"]
    if bad:
        freqs = [{"prompt": "A fact-check found this summary contains a claim absent from its sources.\n\n"
                            f"SOURCES:\n{voice_payload(stories[k])}\n\nSUMMARY:\n{snips[k]}\n\n"
                            f"FACT-CHECK:\n{ver[k]}\n\n"
                            "Rewrite the summary so every statement is supported by the sources. Remove the "
                            "flagged claim entirely rather than softening it. Do not name news organizations. "
                            "Output ONLY the corrected snippet prose.",
                  "system": SYS_SNIP, "model": model, "max_tokens": 400} for k in bad]
        rewritten = []
        for k, r in zip(bad, host.llm(freqs, max_concurrency=6)):
            cand = scrub_attribution((r.get("text") or "").strip() if isinstance(r, dict) else "")
            if cand and not snippet_flags(cand):
                snips[k] = cand
                rewritten.append(k)
        # Only the rewritten snippets changed, so only they can have changed verdict. A
        # snippet that failed and could NOT be rewritten keeps its UNSUPPORTED verdict and
        # still falls through to the sentence-drop path below.
        for k, v in entail(snips, only=rewritten).items():
            ver[k] = v
        # last resort: drop the offending sentence, keeping whatever is verifiable
        dropped = []
        for k in bad:
            v = ver[k]
            if entail_verdict(v) == "supported":
                continue
            claim = v.split(":", 1)[1].strip() if ":" in v else ""
            key = set(re.findall(r"[a-z]{5,}", claim.lower()))
            sents = [x for x in re.split(r"(?<=[.!?])\s+", snips[k]) if x.strip()]
            if len(sents) > 1 and key:
                overlap = [len(key & set(re.findall(r"[a-z]{5,}", x.lower()))) for x in sents]
                kept = [x for x, o in zip(sents, overlap) if o < max(overlap)]
                if kept:
                    snips[k] = " ".join(kept)
                    dropped.append(k)
        for k, v in entail(snips, only=dropped).items():
            ver[k] = v

    flags = [snippet_flags(t) for t in snips]
    return snips, ver, flags


SYS_DUP = ("You decide whether a news article covers the same underlying event as any story in a list. "
           "Same event means the same occurrence -- a different angle, a different headline, or a "
           "follow-up detail on the same occurrence still counts as the SAME event. A different "
           "occurrence involving the same people or organisations is a DIFFERENT event.")


def find_exclusives(host, model, uniq, stories, limit=8, shortlist=5):
    """Single-outlet articles, from any of the 19 panel outlets, that reached no
    multi-outlet story. Broadened 2026-07-27 from a Reuters/WSJ/NYT-only restriction, per
    the user's decision that dropping the fixed voice tier applies here too. A single-outlet
    story is not necessarily a small one -- it may be that paper's exclusive -- so instead of
    discarding them, surface the ones each paper played highest in its own feed.

    Exclusion must be by STORY, not by exact title string. Clustering is imperfect: a
    second NYT article on an event already in the digest (a different angle, a different
    headline) is left out of that cluster, and matching on (source, title) then labels it
    'carried by no other panel outlet' -- contradicting the digest's own prevalence figure
    for the same event. Observed with the SpaceX 13th Starship test, simultaneously a
    5-outlet story and an un-picked-up exclusive.

    A TF-IDF threshold cannot do this screening: measured on that exact case the true
    same-event pair scored 0.164 against a 0.05 noise floor, leaving no safe cut point
    (which is also why clustering missed it). So similarity is used only to SHORTLIST the
    plausible matches, and the same-event decision is adjudicated the way cluster coherence
    is -- by asking the model, which handles rewording that bag-of-words cannot.
    """
    placed = {(m["source"], m["title"]) for s in stories for m in s["members"]}
    lone = [i for i in uniq if i["source"] in PSET and (i["source"], i["title"]) not in placed
            and not is_non_article(i["title"])]
    # feed_rank means "the paper led with it" ONLY for outlets read through their own feed.
    # For Google-transported outlets the order is Google's relevance ranking, so a low rank
    # says nothing about the paper's own play; those are admitted on recency instead, and
    # each item records which basis applied so the section can label it honestly.
    for i in lone:
        i["rank_is_editorial"] = i["source"] in DIRECT_FEED
    pool = [i for i in lone
            if (i["feed_rank"] <= 5 if i["rank_is_editorial"] else i["age_hours"] <= 12)
            and not SOFT_FEATURE.search(i["title"])]
    # Round-robin across all 19 panel outlets (was: the three voice outlets only). Sorting by
    # rank alone hands every slot to outlets with a direct feed (their feed_rank beats any
    # transported item), so Google-transported exclusives -- the least replaceable items here
    # -- never appear.
    by_src = defaultdict(list)
    for i in sorted(pool, key=lambda i: (0 if i["rank_is_editorial"] else 1,
                                         i["feed_rank"] if i["rank_is_editorial"] else i["age_hours"])):
        by_src[i["source"]].append(i)
    # Round-robin keeps this fair across outlets, but the adjudication call below costs one
    # LLM request per candidate for a section that only ever shows `limit` results -- capping
    # total candidates (not just rounds) keeps that cost proportional to what's actually
    # shown. Observed 2026-07-28: with a wide max_age_hours and a large panel, the old
    # rounds-only cap (k>40, i.e. up to 40 items PER OUTLET) admitted 430 candidates to
    # produce 8 exclusives -- 43% of a run's total input tokens for one section. 5x `limit`
    # keeps generous headroom for the dedup pass to drop overlaps and still land on a full
    # `limit`, at roughly a tenth of the calls.
    max_candidates = limit * 5
    cand, k = [], 0
    while any(by_src[s] for s in PANEL) and len(cand) < max_candidates:
        for src_name in PANEL:
            if by_src[src_name]:
                cand.append(by_src[src_name].pop(0))
            if len(cand) >= max_candidates:
                break
        k += 1
        if k > 40:
            break
    if not cand or not stories:
        return cand[:limit], len(lone), []

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    story_txt = [" ".join(f'{m["title"]}. {m["summary"][:200]}' for m in s["members"]) for s in stories]
    cand_txt = [f'{i["title"]}. {i["summary"][:220]}' for i in cand]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
    M = vec.fit_transform(story_txt + cand_txt)
    sim = cosine_similarity(M[len(story_txt):], M[:len(story_txt)])

    shortlists = [list(row.argsort()[::-1][:shortlist]) for row in sim]
    reqs = [{"prompt": "ARTICLE:\n" + f'{i["title"]}\n{i["summary"][:300]}\n\n'
                       + "CANDIDATE STORIES ALREADY REPORTED:\n"
                       + "\n".join(f'{n}. {primary(stories[j])["title"]}' for n, j in enumerate(sl))
                       + '\n\nDoes the ARTICLE cover the same event as any listed story? Reply ONLY '
                         'with JSON: {"same_as": <story number or null>}',
             "system": SYS_DUP, "model": model, "max_tokens": 60}
            for i, sl in zip(cand, shortlists)]
    verdicts = host.llm(reqs, max_concurrency=6)

    out, dropped = [], []
    for i, sl, row, r in zip(cand, shortlists, sim, verdicts):
        same = None
        txt = (r.get("text") or "") if isinstance(r, dict) else ""
        j = extract_json(txt)
        # null is a legitimate verdict ("not a duplicate"), so the contract is satisfied by a
        # parsed object carrying the key -- not by a non-null value.
        ok = isinstance(j, dict) and "same_as" in j
        contract_note("exclusives_dedup", ok, txt)
        if ok:
            val = j.get("same_as")
            if isinstance(val, bool):
                val = None                 # guard: True would otherwise index as 1
            if isinstance(val, int) and 0 <= val < len(sl):
                same = sl[val]
        if same is not None:
            dropped.append((i["title"], primary(stories[same])["title"], round(float(row[same]), 3)))
        else:
            out.append(i)
    return out[:limit], len(lone), dropped


def render(stories, snips, ver, blind, n_scanned, stamp, date_utc, exclusives=None, n_lone=0,
           meta=None, max_age_hours=MAX_AGE_HOURS):
    NP = len(PANEL)
    L = ["=" * 88, f"NEWS OF THE DAY  ·  {date_utc}".center(88),
         f"compiled {stamp} · ranked by breadth of coverage".center(88), "=" * 88, "",
         textwrap.fill(
             f"Scanned {n_scanned} headlines published in the last {max_age_hours}h across a fixed "
             f"panel of {NP} outlets. For each story, snippets are written from whichever panel "
             "outlet(s) are covering it most closely -- not a fixed subset -- and normalized to a "
             "neutral, matter-of-fact tone regardless of any editorializing or framing in the "
             "source material. Google News is used as a transport for paywalled or "
             "bot-protected publishers -- each item is credited to its originating publisher, never "
             "counted as an outlet of its own. Clusters are audited for event coherence, and each "
             "snippet is entailment-checked against its sources and screened for truncation. "
             "Snippets run 1-3 sentences: some stories carry only enough sourced detail for one, "
             "and no sentence is padded to reach a target length.", 86), ""]
    # A feed that failed after retries contributes nothing, so every prevalence count that day
    # understates the outlets that carried the story. Disclose it rather than let the counts imply
    # a full panel was read.
    degraded = (meta or {}).get("degraded_sources") or []
    if degraded:
        L += [textwrap.fill(
            f"CAVEAT: {len(degraded)} panel outlet(s) returned no usable items this run "
            f"({', '.join(degraded)}) after retries. Prevalence counts below are out of {NP} "
            f"but effectively drawn from {NP - len(degraded)}, so stories those outlets would "
            "have carried are undercounted.", 86), ""]
    for rank, (s, txt) in enumerate(zip(stories, snips), 1):
        prim = primary(s)
        leaders, _ = coverage_leaders(s, max_outlets=4)
        others = [o for o in s["outlets"] if o not in leaders]
        L += ["-" * 88, f"{rank}. {prim['title']}",
              f"   PREVALENCE: {s['n_outlets']}/{NP} panel outlets · {s['n_items']} articles",
              f"   COVERED CLOSEST BY: {', '.join(leaders)}",
              f"   ALSO CARRIED BY: {', '.join(others) if others else '— none in panel'}"]
        if s["extra"]:
            L.append(f"   (beyond panel: {', '.join(s['extra'])})")
        L.append("")
        L += ["   " + ln for ln in textwrap.fill(txt, 84).split("\n")]
        # URLs are emitted whole. Google News redirect links run to ~290 chars, so the old
        # [:150] slice silently produced dead links for most transported stories.
        L += ["", f"   link: {prim['link']}", ""]
    if exclusives:
        L += ["=" * 88, "EXCLUSIVES, NOT YET PICKED UP", "=" * 88,
              "Carried by no other panel outlet -- either an exclusive or an early break.",
              f"({n_lone} single-outlet panel articles today, across all 19 outlets.) Marked (led)",
              "where the paper's own feed placed it near the top; unmarked items reach us through",
              "a search feed whose order is not the paper's, so they are listed on recency instead.", ""]
        for i in exclusives:
            tag = " (led)" if i.get("rank_is_editorial") else ""
            L += [f"  · [{i['source']}{tag}] {i['title']}", f"      {i['link']}"]
        L.append("")
    if blind:
        L += ["=" * 88, "RUNNING WIDELY, NO REAL PANEL ARTICLE YET  (3+ panel outlets)", "=" * 88]
        for s in blind[:8]:
            L += [f"  · [{s['n_outlets']}/{NP}] {primary(s)['title']}",
                  f"      {', '.join(s['outlets'])}"]
        L.append("")
    malformed = [k + 1 for k, t in enumerate(snips) if [f for f in snippet_flags(t)
                                                        if f != "single_sentence"]]
    L += ["=" * 88, f"Panel ({NP}): {', '.join(PANEL)}"]
    if malformed:
        L.append(f"NOTE -- snippets flagged as malformed after repair: {malformed}")
    if meta:
        L.append(f"Freshness filter: dropped {meta['n_stale']} items older than {max_age_hours}h "
                 f"and {meta['n_undated']} undated, from {meta['n_raw']} fetched.")
        if meta["stale_by_source"]:
            worst = sorted(meta["stale_by_source"].items(), key=lambda kv: -kv[1])[:4]
            L.append("  most stale: " + ", ".join(f"{k} {v}" for k, v in worst))
        if meta.get("degraded_sources"):
            L.append("  WARNING -- barely represented today (possible feed failure): "
                     + ", ".join(meta["degraded_sources"]))
    L.append("=" * 88)
    return "\n".join(L)


def publish_scan(result):
    """Make this run's scan metadata live, atomically.

    Call AFTER the digest CSV is on disk. Until then raw_headlines_<date>.json still holds the
    PREVIOUS run's numbers, which is correct: they are the numbers describing the rows the site
    is still serving. os.replace is atomic on both POSIX and Windows, so a reader polling
    /api/digest sees either the old scan or the new one -- never a half-written file.

    A run that dies before this point leaves the previous scan in place and the partial behind
    for inspection, so a crashed run cannot restate the caption over stale rows.
    """
    partial = result.get("scan_partial")
    if not partial or not pathlib.Path(partial).exists():
        return False
    os.replace(partial, result["scan_final"])
    return True


def run(host, top_n=18, max_age_hours=MAX_AGE_HOURS):
    contract_reset()          # server.py is long-lived; counters are per-run, not per-process
    uniq, meta = fetch_all(max_age_hours)
    errs = meta["errors"]
    now = datetime.datetime.now(datetime.timezone.utc)
    date_utc, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M UTC")
    raw_path = pathlib.Path(f"raw_headlines_{date_utc}.json")
    # Written to a PARTIAL path, not raw_path, and published only after the CSV it describes
    # (see publish_scan(); the runners call it right after write_csv). The site reads
    # max_age_hours, n_raw, n_stale and the per-outlet counts out of this file to caption the
    # digest -- "anything published in the last 36 hours", "Scanned N headlines". Overwriting it
    # at FETCH time made those numbers describe the run now starting while the page still showed
    # the PREVIOUS run's stories: change the window to 3h and the caption immediately claimed a
    # 3-hour window over stories gathered under the old one. The caption must describe the rows
    # on screen, so the new numbers stay in the partial until the new rows are on disk.
    raw_partial = raw_path.with_name(raw_path.name + ".partial")
    raw_payload = {"fetched_utc": now.isoformat(timespec="seconds"), "max_age_hours": max_age_hours,
                   "meta": meta, "items": uniq}
    raw_partial.write_text(json.dumps(raw_payload, ensure_ascii=False), encoding="utf-8")
    model = host.reasoning_model()
    stories = audit_coherence(host, cluster(uniq), model)
    # coherence SPLITS multi-event clusters; merge_fragments does the converse, re-joining one
    # event scattered across differently-worded coverage. Both are needed: the first stops a
    # cluster overstating prevalence, the second stops fragmentation understating it.
    stories, n_merged = merge_fragments(host, stories, model)
    # require a real ARTICLE from some panel outlet, not merely a podcast/live-blog entry:
    # otherwise primary() falls through and the row is headlined by non-covering material
    top = [s for s in stories if real_panel_members(s)][:top_n]
    blind = [s for s in stories if not real_panel_members(s) and s["n_outlets"] >= 3
             and not is_non_article(primary(s)["title"])]
    snips, ver, sflags = write_snippets(host, top, model)
    # exclusives are screened against the stories actually REPORTED (top + blind), since a
    # candidate matching a story that never made the digest is still genuinely unreported here
    exclusives, n_lone, exc_dropped = find_exclusives(host, model, uniq, top + blind)
    digest = render(top, snips, ver, blind, len(uniq), stamp, date_utc, exclusives, n_lone,
                    meta, max_age_hours)
    pathlib.Path(f"news_digest_{date_utc}.txt").write_text(digest, encoding="utf-8")
    rows = []
    for r, (s, t, v, fl) in enumerate(zip(top, snips, ver, sflags), 1):
        leaders, _ = coverage_leaders(s, max_outlets=4)
        links = outlet_links(s)
        rows.append({
            "rank": r, "headline": primary(s)["title"], "n_panel_outlets": s["n_outlets"],
            "panel_size": len(PANEL), "n_articles": s["n_items"],
            "covered_closest_by": "; ".join(leaders),
            "also_carried_by": "; ".join(o for o in s["outlets"] if o not in leaders),
            "snippet": t, "entailment": ("clean" if entail_verdict(v) == "supported"
                                         else ("unverified: no verdict returned"
                                               if entail_verdict(v) is None else v[:120])),
            "snippet_form": "ok" if not fl else ";".join(fl),
            "link": primary(s)["link"],
            # one representative URL per covering outlet -- the site renders one link button
            # per outlet from this, so each outlet's own link (not just the primary's) reaches
            # the reader.
            "outlet_links": json.dumps(links, ensure_ascii=False)})
    # fetched_utc marks when feeds were READ, at the START of a run that can spend minutes on
    # clustering/snippet-writing/entailment-checking; a "Refreshed" banner using it alone reads
    # several minutes stale the instant a user-triggered refresh finishes. completed_utc is
    # explicitly stored here (not filesystem mtime, which this file already avoids relying on
    # for the same copy/sync-safety reason) so it survives the same hazards fetched_utc does,
    # while actually matching when THIS run's own output became current.
    raw_payload["completed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    raw_payload["meta"] = meta
    raw_partial.write_text(json.dumps(raw_payload, ensure_ascii=False), encoding="utf-8")
    meta["contract"] = contract_report()
    return {"scan_partial": str(raw_partial), "scan_final": str(raw_path),
            "date": date_utc, "stamp": stamp, "n_scanned": len(uniq), "errors": errs,
            "stories": stories, "top": top, "blind": blind, "rows": rows, "digest": digest,
            "exclusives": exclusives, "n_lone": n_lone, "exc_dropped": exc_dropped,
            "n_merged": n_merged, "meta": meta, "items": uniq}
