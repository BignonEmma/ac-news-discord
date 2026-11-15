#!/usr/bin/env python3
"""Surveille des sous-forums ForumActif et annonce les nouveaux sujets sur Discord."""
import json, os, re, sys, time, urllib.request, urllib.error
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (compatible; AstraeliaNotifier/1.0)"
TOPIC_HREF = re.compile(r"^(?:https?://[^/]+)?/t(\d+)-([^/?#]*)(?:[?#].*)?$")


class TopicParser(HTMLParser):
    """Récupère les liens de sujets (/t123-titre) avec leur texte.
    in_list = True si au moins un lien de ce sujet porte la classe topictitle
    (= vraie liste des sujets, pas les encarts 'derniers sujets' de la page)."""

    def __init__(self):
        super().__init__()
        self.topics = {}  # id -> {"slug","title","in_list"}
        self._cur = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        m = TOPIC_HREF.match(a.get("href") or "")
        if m:
            self._cur = {"id": m.group(1), "slug": m.group(2),
                         "is_title": "topictitle" in (a.get("class") or ""), "text": ""}

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data

    def handle_endtag(self, tag):
        if tag != "a" or self._cur is None:
            return
        c, self._cur = self._cur, None
        text = " ".join(c["text"].split())
        t = self.topics.setdefault(c["id"], {"slug": c["slug"], "title": "", "in_list": False, "title_is_main": False})
        if c["is_title"]:
            t["in_list"] = True
        if text and (c["is_title"] and not t["title_is_main"] or not t["title"]):
            t["title"] = text
            t["title_is_main"] = t["title_is_main"] or c["is_title"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def get_topics(url):
    html = fetch(url) if not os.environ.get("HTML_FILE") else open(os.environ["HTML_FILE"], encoding="utf-8").read()
    p = TopicParser()
    p.feed(html)
    in_list = {i: t for i, t in p.topics.items() if t["in_list"]}
    print("Liens de sujets trouvés sur la page: %d, dont %d dans la vraie liste." % (len(p.topics), len(in_list)))
    if not in_list:
        raise RuntimeError("aucun titre de sujet reconnu (classe topictitle absente): la page a changé ou n'est pas celle attendue")
    out = {}
    for tid, t in in_list.items():
        title = t["title"] or unquote(t["slug"]).replace("-", " ").strip().capitalize()
        out[tid] = {"title": title, "url": urljoin(url, "/t%s-%s" % (tid, t["slug"]))}
    return out


def send_discord(webhook, forum, topic, existing=False):
    key_msg, key_label = ("existing_message", "existing_label") if existing else ("message", "label")
    msg = forum.get(key_msg, forum.get("message", "Un nouveau sujet vient d'être posté !")).format(name=forum["name"])
    payload = {
        "username": forum.get("bot_name", "Astraelia"),
        "content": forum.get("mention", ""),
        "embeds": [{
            "title": topic["title"],
            "url": topic["url"],
            "description": msg,
            "color": forum.get("color", 0x7B5EA7),
            "author": {"name": forum.get(key_label, forum.get("label", "Nouveau sujet"))},
        }],
    }
    if DRY_RUN:
        print("[DRY_RUN]", json.dumps(payload, ensure_ascii=False))
        return
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        if e.code == 429:  # rate limit Discord
            time.sleep(float(e.headers.get("Retry-After", "5")) + 1)
            urllib.request.urlopen(req, timeout=30).read()
        else:
            raise


def main():
    config = json.load(open(CONFIG_FILE, encoding="utf-8"))
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    failed = False

    for forum in config["forums"]:
        key = forum["url"]
        webhook = os.environ.get(forum["webhook_secret"], "")
        if not webhook and not DRY_RUN:
            print("Secret manquant: %s" % forum["webhook_secret"]); failed = True; continue
        try:
            topics = get_topics(forum["url"])
        except Exception as e:
            print("Lecture impossible pour %s: %s" % (key, e)); failed = True; continue
        if not topics:
            print("Aucun sujet trouvé sur %s, la page a peut-être changé. État inchangé." % key)
            failed = True; continue

        print("Sujets lus: " + ", ".join(sorted(topics, key=int)))
        skip = set(str(i) for i in forum.get("skip_ids", []))
        topics = {i: t for i, t in topics.items() if i not in skip}
        seen = set(state.get(key, []))
        existing_run = False
        if key not in state:
            if forum.get("announce_existing"):
                print("Première exécution pour %s: annonce des %d sujets déjà présents." % (forum["name"], len(topics)))
                seen = set()
                existing_run = True
            else:
                print("Première exécution pour %s: %d sujets enregistrés sans annonce." % (forum["name"], len(topics)))
                state[key] = sorted(topics, key=int)
                continue

        new_ids = sorted((i for i in topics if i not in seen), key=int)
        for tid in new_ids:
            try:
                send_discord(webhook, forum, topics[tid], existing_run)
                seen.add(tid)
                print("Annoncé: %s" % topics[tid]["title"])
                time.sleep(1)
            except Exception as e:
                print("Échec d'envoi pour %s: %s (sera retenté)" % (tid, e)); failed = True
        state[key] = sorted(seen, key=int)

    if not DRY_RUN:
        json.dump(state, open(STATE_FILE, "w"), indent=1)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
