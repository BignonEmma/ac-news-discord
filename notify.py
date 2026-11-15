#!/usr/bin/env python3
"""Surveille des sous-forums et des sujets ForumActif et annonce les nouveautés sur Discord.

- "forums": annonce les NOUVEAUX SUJETS d'un sous-forum (ex: nouvelles fiches).
- "topics": annonce les NOUVEAUX MESSAGES d'un sujet précis, avec leur contenu.
"""
import json, os, re, sys, time, urllib.request, urllib.error
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (compatible; AstraeliaNotifier/1.0)"
TOPIC_HREF = re.compile(r"^(?:https?://[^/]+)?/t(\d+)-([^/?#]*)(?:[?#].*)?$")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "tr", "table", "ul", "ol", "section", "article", "center", "dl", "dd", "dt"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


# ---------------------------------------------------------------- réseau / Discord
def fetch(url):
    fake = os.environ.get("FAKE_PAGES")  # pour les tests hors ligne
    if fake:
        m = json.loads(fake)
        if url in m:
            return open(m[url], encoding="utf-8").read()
        raise RuntimeError("page de test inconnue: " + url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def get_secret(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        return json.loads(os.environ.get("ALL_SECRETS") or "{}").get(name, "")
    except ValueError:
        return ""


def post_webhook(webhook, payload):
    payload.setdefault("allowed_mentions", {"parse": []})  # jamais de ping @everyone/@here/rôle venant du forum
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
        if e.code == 429:  # limite de débit Discord
            time.sleep(float(e.headers.get("Retry-After", "5")) + 1)
            urllib.request.urlopen(req, timeout=30).read()
        else:
            raise


# ---------------------------------------------------------------- nouveaux SUJETS d'un sous-forum
def _ident(tag, attrs):
    a = dict(attrs)
    out = tag
    if a.get("id"):
        out += "#" + a["id"]
    if a.get("class"):
        out += "." + ".".join(a["class"].split()[:3])
    return out


class TopicParser(HTMLParser):
    """Récupère les liens de sujets (/t123-titre) de la vraie liste (pas des encarts ni des cartes épinglées)."""

    def __init__(self, title_class="tr-title", container_class="tr-list", exclude_classes=()):
        super().__init__()
        self.title_class = title_class
        self.container_class = container_class
        self.exclude = set(exclude_classes)
        self.topics = {}
        self.links = []
        self.stack = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            m = TOPIC_HREF.match(a.get("href") or "")
            if m:
                in_box = (not self.container_class) or any(self.container_class in x[2] for x in self.stack)
                excluded = any(self.exclude & set(x[2]) for x in self.stack)
                is_title = (not self.title_class or self.title_class in (a.get("class") or "").split()) and in_box and not excluded
                self._cur = {"id": m.group(1), "slug": m.group(2), "cls": a.get("class") or "",
                             "is_title": is_title, "text": "",
                             "anc": " > ".join(x[1] for x in self.stack[-6:])}
        if tag not in VOID:
            self.stack.append((tag, _ident(tag, attrs), (a.get("class") or "").split()))

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag != "a" or self._cur is None:
            return
        c, self._cur = self._cur, None
        text = " ".join(c["text"].split())
        self.links.append({"id": c["id"], "cls": c["cls"], "anc": c["anc"], "text": text})
        t = self.topics.setdefault(c["id"], {"slug": c["slug"], "title": "", "in_list": False, "title_is_main": False})
        if c["is_title"]:
            t["in_list"] = True
        if text and (c["is_title"] and not t["title_is_main"] or not t["title"]):
            t["title"] = text
            t["title_is_main"] = t["title_is_main"] or c["is_title"]


def get_topics(url, forum=None):
    forum = forum or {}
    html = fetch(url)
    p = TopicParser(forum.get("title_class", "tr-title"), forum.get("container_class", "tr-list"),
                    forum.get("exclude_classes", ["tc-grid", "folder_announce", "folder_sticky", "folder_global"]))
    p.feed(html)
    in_list = {i: t for i, t in p.topics.items() if t["in_list"]}
    print("Liens de sujets trouvés sur la page: %d, dont %d dans la vraie liste." % (len(p.topics), len(in_list)))
    if not in_list:
        print("--- DIAGNOSTIC: un lien par ligne (numéro | classe du lien | environnement dans la page | texte) ---")
        for l in p.links:
            print("t%s | a.%s | %s | %s" % (l["id"], l["cls"] or "-", l["anc"], l["text"][:40]))
        print("--- FIN DIAGNOSTIC ---")
        raise RuntimeError("aucun sujet reconnu (lien de classe %s dans un bloc %s): la page a changé ou n'est pas celle attendue" % (p.title_class, p.container_class))
    out = {}
    for tid, t in in_list.items():
        title = t["title"] or unquote(t["slug"]).replace("-", " ").strip().capitalize()
        out[tid] = {"title": title, "url": urljoin(url, "/t%s-%s" % (tid, t["slug"]))}
    return out


def send_discord(webhook, forum, topic, existing=False):
    key_msg, key_label = ("existing_message", "existing_label") if existing else ("message", "label")
    msg = forum.get(key_msg, forum.get("message", "Un nouveau sujet vient d'être posté !")).format(name=forum["name"])
    post_webhook(webhook, {
        "username": forum.get("bot_name", "Astraelia"),
        "content": forum.get("mention", ""),
        "embeds": [{
            "title": topic["title"][:256],
            "url": topic["url"],
            "description": msg,
            "color": forum.get("color", 0x7B5EA7),
            "author": {"name": forum.get(key_label, forum.get("label", "Nouveau sujet"))},
        }],
    })


# ---------------------------------------------------------------- nouveaux MESSAGES d'un sujet
class PostParser(HTMLParser):
    """Lit les messages d'une page de sujet: numéro, auteur, date, lien direct, contenu (converti en texte Discord)."""

    def __init__(self, base_url):
        super().__init__()
        self.base = base_url
        self.stack = []
        self.posts = []
        self.post = None
        self.post_depth = 0
        self.mode = None
        self.mode_depth = 0
        self.buf = []
        self.skip = 0
        self.in_time = False
        self.links = []
        self.quotes = []
        self.pre = 0
        self.images = []

    # -- utilitaires
    def _nl(self):
        if self.buf and not "".join(self.buf[-2:]).endswith("\n"):
            self.buf.append("\n")

    def _abs(self, href):
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            return ""
        return urljoin(self.base, href)

    # -- balises ouvrantes
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "br":
            if self.mode == "content" and not self.skip:
                self._nl() if self.pre == 0 else self.buf.append("\n")
            return
        if tag in VOID:
            if tag == "img" and self.mode == "content" and not self.skip:
                src = self._abs(a.get("src"))
                if src and not any(k in src for k in ("smiles", "smiley", "emoji", "twemoji", "/fa/i/")):
                    self.images.append(src)
            return
        self.stack.append(tag)
        depth = len(self.stack)
        if tag in ("style", "script"):
            self.skip += 1
            return
        if self.post is None:
            if tag == "div" and "post_row" in cls and (a.get("id") or "").isdigit():
                self.post = {"id": int(a["id"]), "author": "", "date": "", "url": "", "text": "", "images": []}
                self.post_depth = depth
            return
        if self.mode is None:
            if tag == "h2" and "vt-post-name" in cls:
                self.mode, self.mode_depth, self.buf = "author", depth, []
            elif "post_date" in cls:
                self.mode, self.mode_depth, self.buf = "date", depth, []
            elif tag == "div" and "post_message" in cls:
                self.mode, self.mode_depth, self.buf = "content", depth, []
                self.links, self.quotes, self.pre, self.images = [], [], 0, []
            return
        if self.mode == "date":
            if tag == "time":
                self.in_time = True
            elif tag == "a" and not self.post["url"]:
                self.post["url"] = self._abs(a.get("href"))
            return
        if self.mode == "content" and not self.skip:
            if tag in ("b", "strong"):
                self.buf.append("**")
            elif tag in ("i", "em"):
                self.buf.append("*")
            elif tag == "u":
                self.buf.append("__")
            elif tag in ("s", "strike", "del"):
                self.buf.append("~~")
            elif tag in HEADINGS:
                self._nl(); self.buf.append("**")
            elif tag == "li":
                self._nl(); self.buf.append("• ")
            elif tag == "a":
                self.links.append((len(self.buf), a.get("href") or ""))
                self.buf.append("[")
            elif tag == "pre":
                self._nl(); self.buf.append("```\n"); self.pre += 1
            elif tag == "code" and not self.pre:
                self.buf.append("`")
            elif tag == "blockquote":
                self._nl(); self.quotes.append(len(self.buf))
            elif tag in BLOCK:
                self._nl()

    def handle_data(self, data):
        if self.skip or self.mode is None:
            return
        if self.mode == "date" and not self.in_time:
            return
        if self.mode == "content" and not self.pre:
            data = re.sub(r"\s+", " ", data)
        self.buf.append(data)

    # -- balises fermantes
    def handle_endtag(self, tag):
        if tag in VOID or tag not in self.stack:
            return
        while self.stack:
            depth = len(self.stack)
            t = self.stack.pop()
            self._closing(t, depth)
            if t == tag:
                break

    def _closing(self, t, depth):
        if t in ("style", "script"):
            self.skip -= 1
            return
        if self.post is None:
            return
        if self.mode == "content" and depth > self.mode_depth and not self.skip:
            if t in ("b", "strong"):
                self.buf.append("**")
            elif t in ("i", "em"):
                self.buf.append("*")
            elif t == "u":
                self.buf.append("__")
            elif t in ("s", "strike", "del"):
                self.buf.append("~~")
            elif t in HEADINGS:
                self.buf.append("**"); self._nl()
            elif t == "a" and self.links:
                start, href = self.links.pop()
                seg = "".join(self.buf[start + 1:]).strip()
                url = self._abs(href).replace("(", "%28").replace(")", "%29")
                if not seg:
                    del self.buf[start:]
                elif url:
                    self.buf.append("](%s)" % url)
                else:
                    self.buf[start] = ""
            elif t == "pre":
                self.buf.append("\n```\n"); self.pre = max(0, self.pre - 1)
            elif t == "code" and not self.pre:
                self.buf.append("`")
            elif t == "blockquote" and self.quotes:
                mark = self.quotes.pop()
                seg = "".join(self.buf[mark:]).strip("\n")
                del self.buf[mark:]
                self.buf.append("\n" + "\n".join(("> " + l) if l.strip() else ">" for l in seg.split("\n")) + "\n")
            elif t == "p":
                self._nl()
                if not "".join(self.buf[-3:]).endswith("\n\n"):
                    self.buf.append("\n")
            elif t in BLOCK or t == "li":
                self._nl()
        if self.mode and depth == self.mode_depth:
            text = "".join(self.buf)
            if self.mode in ("author", "date"):
                self.post[self.mode] = " ".join(text.split())
                self.in_time = False
            else:
                lines = [l.strip() for l in text.split("\n")]
                text = "\n".join(lines)
                text = re.sub(r"(\*\*|__|~~)\s*\1", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                self.post["text"] = text
                self.post["images"] = list(self.images)
            self.mode = None
        if depth == self.post_depth:
            self.posts.append(self.post)
            self.post = None


def parse_posts(html, base_url):
    p = PostParser(base_url)
    p.feed(html)
    return sorted(p.posts, key=lambda x: x["id"])


def page_offsets(html, tid):
    """Décalages des pages d'un sujet (liens /t72p15-...), complétés avec le pas détecté."""
    found = sorted({int(x) for x in re.findall(r'href="(?:https?://[^"/]+)?/t%sp(\d+)-' % tid, html)})
    if not found:
        return []
    step = found[0]
    return list(range(step, found[-1] + 1, step)) if step > 0 else found


def chunk_text(text, limit=3800, max_chunks=4):
    chunks = []
    while text and len(chunks) < max_chunks:
        if len(text) <= limit:
            chunks.append(text)
            text = ""
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return (chunks or ["(message sans texte)"]), bool(text)


def send_post(webhook, w, post):
    author = re.sub(r"([*_~`|\\])", r"\\\1", post["author"] or "un membre")
    link = post["url"] or "%s#%d" % (w["url"], post["id"])
    chunks, truncated = chunk_text(post["text"])
    if truncated:
        chunks[-1] += "\n\n[… suite du message sur le forum](%s)" % link
    for i, chunk in enumerate(chunks):
        emb = {"description": chunk, "color": w.get("color", 0x4E7AC7)}
        if i == 0:
            emb["author"] = {"name": w.get("label", "Nouveau message")}
            emb["title"] = ("Un message de %s a été posté dans %s" % (author, w["name"]))[:256]
            emb["url"] = link
            if post["date"]:
                emb["footer"] = {"text": post["date"]}
        else:
            emb["footer"] = {"text": "suite %d/%d" % (i + 1, len(chunks))}
        if i == len(chunks) - 1 and post["images"]:
            emb["image"] = {"url": post["images"][0]}
        post_webhook(webhook, {"username": w.get("bot_name", "Astraelia"), "embeds": [emb]})
        time.sleep(1)


def watch_topic(w, state, webhook):
    url = w["url"]
    m = re.search(r"/t(\d+)-([^/?#]*)", url)
    if not m:
        raise RuntimeError("adresse de sujet invalide: " + url)
    tid, slug = m.groups()
    key = "posts:" + url
    first = fetch(url)
    offs = page_offsets(first, tid)
    page_urls = [url] + [urljoin(url, "/t%sp%d-%s" % (tid, o, slug)) for o in offs]
    last_idx = len(page_urls) - 1
    posts = parse_posts(first if last_idx == 0 else fetch(page_urls[last_idx]), url)
    print("Sujet %s: %d page(s), %d message(s) lus sur la dernière." % (w["name"], len(page_urls), len(posts)))
    if not posts:
        raise RuntimeError("aucun message reconnu dans le sujet %s (la page a changé ?)" % w["name"])
    if key not in state:
        state[key] = max(p["id"] for p in posts)
        print("Première exécution pour %s: dernier message n°%d noté, sans annonce." % (w["name"], state[key]))
        return True
    last = int(state[key])
    new = [p for p in posts if p["id"] > last]
    idx = last_idx
    while idx > 0 and posts and not any(p["id"] <= last for p in posts) and last_idx - idx < 5:
        idx -= 1
        posts = parse_posts(fetch(page_urls[idx]), url)
        new += [p for p in posts if p["id"] > last]
    ok = True
    for p in sorted({p["id"]: p for p in new}.values(), key=lambda x: x["id"]):
        try:
            send_post(webhook, w, p)
            state[key] = p["id"]
            print("Annoncé: message n°%d de %s dans %s" % (p["id"], p["author"], w["name"]))
        except Exception as e:
            print("Échec d'envoi du message n°%d: %s (sera retenté)" % (p["id"], e))
            ok = False
            break
    return ok


# ---------------------------------------------------------------- programme principal
def main():
    config = json.load(open(CONFIG_FILE, encoding="utf-8"))
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    failed = False

    for forum in config.get("forums", []):
        key = forum["url"]
        webhook = get_secret(forum["webhook_secret"])
        if not webhook and not DRY_RUN:
            print("Secret manquant: %s" % forum["webhook_secret"]); failed = True; continue
        try:
            topics = get_topics(forum["url"], forum)
        except Exception as e:
            print("Lecture impossible pour %s: %s" % (key, e)); failed = True; continue
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

    for w in config.get("topics", []):
        webhook = get_secret(w["webhook_secret"])
        if not webhook and not DRY_RUN:
            print("Secret manquant: %s" % w["webhook_secret"]); failed = True; continue
        try:
            if not watch_topic(w, state, webhook):
                failed = True
        except Exception as e:
            print("Lecture impossible pour %s: %s" % (w["url"], e)); failed = True

    if not DRY_RUN:
        json.dump(state, open(STATE_FILE, "w"), indent=1)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
