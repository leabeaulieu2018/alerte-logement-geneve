"""
Agent d'alerte logement Genève — version hybride.

Détection (GRATUITE, sans IA) :
  à chaque passage, on liste les liens de la page de résultats de chaque régie.
  Un lien jamais vu auparavant = une nouvelle annonce probable.

Analyse de l'annonce :
  - avec ANTHROPIC_API_KEY : Claude lit la fiche (contact, visites, loyer, NPA) et filtre finement ;
  - sans clé : extraction par motifs (CHF, téléphones, e-mails, lignes "visite").
"""

import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from urllib import robotparser
from urllib.parse import urlparse, urldefrag

import yaml
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "state.json")
DIAG_FILE = os.path.join(ROOT, "diagnostic.json")
CONFIG_FILE = os.path.join(ROOT, "config.yaml")

USER_AGENT = "Mozilla/5.0 (compatible; AlerteLogementGeneve/1.0; usage personnel)"
PAUSE = 3                 # secondes entre deux pages
MAX_NOUVEAUX = 15         # au-delà, on suppose un changement du site, pas 15 annonces d'un coup
MAX_CHARS = 25000

USE_CLAUDE = bool(os.environ.get("ANTHROPIC_API_KEY"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
if USE_CLAUDE:
    import anthropic
    client = anthropic.Anthropic()

# Liens qui ne sont jamais des annonces
EXCLUS = re.compile(
    r"(mailto:|tel:|javascript:|\.pdf$|\.jpg$|\.png$|login|connexion|contact|actualit|news|blog|"
    r"carriere|emploi|jobs|mentions|privacy|confidentialit|cookies|facebook|instagram|linkedin|"
    r"youtube|twitter|x\.com|google|apple\.com|acheter|vente|/buy|parking|depot|commercial)",
    re.I,
)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_robots = {}


def autorise(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[base] = rp
    return _robots[base] is None or _robots[base].can_fetch(USER_AGENT, url)


def domaine_racine(url):
    host = urlparse(url).netloc.lower()
    return ".".join(host.split(".")[-2:])


# --------------------------------------------------------------------------- navigation

def lire_page(browser, url):
    page = browser.new_page(user_agent=USER_AGENT, locale="fr-CH")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:                                     # attend la fin du chargement, sans bloquer
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            page.wait_for_timeout(5000)          # site "bavard" (ex. Grange) : on continue quand même
        for _ in range(5):                       # charge les annonces en "lazy loading"
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(800)
        texte = page.inner_text("body")
        titre = page.title()
        liens = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [(e.innerText || '').trim().slice(0, 120), e.href])"
        )
        return titre, texte, liens
    finally:
        page.close()


def liens_annonces(regie, liens):
    """Liens candidats : même site, pas de navigation évidente, filtrés par 'motif' si défini."""
    racine = domaine_racine(regie["url"])
    motif = re.compile(regie["motif"], re.I) if regie.get("motif") else None
    page_resultats = urldefrag(regie["url"])[0]
    res = {}
    for texte, href in liens:
        href = urldefrag(href)[0].rstrip("/")
        if not href.startswith("http") or domaine_racine(href) != racine:
            continue
        if href == page_resultats.rstrip("/"):
            continue
        if motif:
            if not motif.search(href):
                continue
        elif EXCLUS.search(href):
            continue
        res[href] = texte
    return res


# --------------------------------------------------------------------------- analyse d'une annonce

RE_TEL = re.compile(r"(?:\+41|0041|\b0)\s?\(?0?\)?\s?\d{2}[\s.]?\d{3}[\s.]?\d{2}[\s.]?\d{2}")
RE_MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
RE_MONTANT = re.compile(r"(\d{1,2}[’'\s.]?\d{3}|\d{3,4})(?:[.,]\d{2}|\.-|\.–)?")


def nombre(s):
    return int(re.sub(r"[^\d]", "", s))


def analyse_sans_ia(texte):
    lignes = [l.strip() for l in texte.splitlines() if l.strip()]
    loyer = None
    for l in lignes:                                # 1re ligne "loyer" qui contient un montant plausible
        if re.search(r"loyer|prix|chf", l, re.I):
            for m in RE_MONTANT.findall(l):
                v = nombre(m)
                if 300 <= v <= 10000:
                    loyer = v
                    break
        if loyer:
            break
    cles = re.compile(r"pi[eè]ces?|m²|m2|[ée]tage|disponib|charges", re.I)
    return {
        "loyer_total_chf": loyer,
        "contact": {
            "telephone": ", ".join(list(dict.fromkeys(RE_TEL.findall(texte)))[:2]) or None,
            "email": ", ".join(list(dict.fromkeys(RE_MAIL.findall(texte)))[:2]) or None,
        },
        "lignes_visite": [l[:200] for l in lignes if re.search(r"visite", l, re.I)][:4],
        "extrait": [l[:160] for l in lignes if cles.search(l)][:8],
    }


def analyse_claude(regie, url, texte):
    prompt = f"""Page web de la régie « {regie['nom']} » ({url}) :

{texte[:MAX_CHARS]}

Retourne UNIQUEMENT un objet JSON :
{{
  "est_annonce_location_logement": true/false (false si ce n'est pas la fiche d'UN logement à louer),
  "adresse": "string ou null",
  "npa": "code postal 4 chiffres ou null",
  "pieces": nombre ou null,
  "surface_m2": nombre ou null,
  "etage": "string ou null",
  "loyer_net_chf": nombre ou null,
  "charges_chf": nombre ou null,
  "loyer_total_chf": nombre ou null,
  "disponibilite": "string ou null",
  "contact": {{"nom": "string ou null", "telephone": "string ou null", "email": "string ou null"}},
  "visites": [{{"date": "string", "heure": "string ou null", "details": "string ou null"}}],
  "comment_postuler": "string ou null",
  "resume": "2 phrases max"
}}
N'invente rien : null si absent."""
    msg = client.messages.create(
        model=MODEL, max_tokens=1500,
        system="Tu réponds uniquement avec du JSON valide, sans texte autour.",
        messages=[{"role": "user", "content": prompt}],
    )
    t = "".join(b.text for b in msg.content if b.type == "text").strip()
    t = t.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(t)


def correspond(d, texte, c):
    loyer = d.get("loyer_total_chf") or d.get("loyer_net_chf")
    if loyer:
        if c.get("loyer_min") and loyer < c["loyer_min"]:
            return False
        if c.get("loyer_max") and loyer > c["loyer_max"]:
            return False
    npas = [str(n) for n in c.get("npa") or []]
    if npas:
        if d.get("npa"):                            # mode Claude : NPA précis
            if str(d["npa"]) not in npas:
                return False
        elif not any(re.search(rf"\b{n}\b", texte) for n in npas):   # mode sans IA
            return False
    p = d.get("pieces")
    if p:
        if c.get("pieces_min") and p < c["pieces_min"]:
            return False
        if c.get("pieces_max") and p > c["pieces_max"]:
            return False
    return True


# --------------------------------------------------------------------------- e-mail

def v(x, defaut="—"):
    return html.escape(str(x)) if x not in (None, "", []) else defaut


def envoyer_mail(regie, url, titre, d):
    contact = d.get("contact") or {}
    loyer = d.get("loyer_total_chf") or d.get("loyer_net_chf")
    if d.get("visites"):
        visites = "".join(f"<li><b>{v(x.get('date'))}</b> {v(x.get('heure'), '')} {v(x.get('details'), '')}</li>"
                          for x in d["visites"])
    elif d.get("lignes_visite"):
        visites = "".join(f"<li>{v(l)}</li>" for l in d["lignes_visite"])
    else:
        visites = "<li>Aucune visite repérée : voir l'annonce</li>"

    blocs = ""
    if d.get("comment_postuler"):
        blocs += f"<h3>Comment postuler</h3><p>{v(d['comment_postuler'])}</p>"
    details = [("Adresse", d.get("adresse")), ("Pièces", d.get("pieces")), ("Surface", d.get("surface_m2")),
               ("Étage", d.get("etage")), ("Charges", d.get("charges_chf")), ("Disponibilité", d.get("disponibilite"))]
    details = "".join(f"{k} : {v(x)}<br>" for k, x in details if x)
    if details:
        blocs += f"<h3>Détails</h3><p>{details}</p>"
    if d.get("resume"):
        blocs += f"<p style='color:#555'>{v(d['resume'])}</p>"
    if d.get("extrait"):
        blocs += "<h3>Extrait de l'annonce</h3><ul>" + "".join(f"<li>{v(l)}</li>" for l in d["extrait"]) + "</ul>"

    corps = f"""<div style="font-family:Arial,sans-serif;font-size:14px;max-width:600px">
<h2 style="margin:0 0 6px">{v(titre)}</h2>
<p style="margin:0 0 12px;color:#555"><b>{v(regie['nom'])}</b> · Loyer repéré : {v(loyer)} CHF</p>
<p><a href="{html.escape(url)}" style="background:#1a56db;color:#fff;padding:10px 16px;border-radius:6px;
text-decoration:none">Ouvrir l'annonce</a></p>
<h3>Visites</h3><ul>{visites}</ul>
<h3>Contact</h3><p>{v(contact.get('nom'), '')}<br>📞 {v(contact.get('telephone'))}<br>✉️ {v(contact.get('email'))}</p>
{blocs}
<p style="color:#999;font-size:12px">Extraction automatique : vérifie toujours sur l'annonce.</p></div>"""

    msg = EmailMessage()
    msg["Subject"] = f"🏠 {regie['nom']} · {loyer or '?'} CHF · {titre[:70]}"
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(f"Nouvelle annonce {regie['nom']} : {url}")
    msg.add_alternative(corps, subtype="html")

    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
    smtp = smtplib.SMTP_SSL(host, port) if port == 465 else smtplib.SMTP(host, port)
    with smtp as s:
        if port != 465:
            s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


# --------------------------------------------------------------------------- boucle principale

def main():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    crit = config["criteres"]
    regies = [r for r in config["regies"] if r.get("url")]
    state = load_json(STATE_FILE, {"vus": {}})
    diag = {}
    envoyes = 0
    log(f"Mode : {'Claude' if USE_CLAUDE else 'sans IA'} · {len(regies)} régies")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for regie in regies:
            nom = regie["nom"]
            try:
                if not autorise(regie["url"]):
                    log(f"{nom} : robots.txt interdit l'accès, ignorée.")
                    continue
                _, _, liens = lire_page(browser, regie["url"])
                time.sleep(PAUSE)
                candidats = liens_annonces(regie, liens)
                diag[nom] = [f"{t} -> {h}" for h, t in candidats.items()]

                vus = state["vus"].setdefault(nom, {})
                premiere_fois = not vus
                nouveaux = [h for h in candidats if h not in vus]
                now = datetime.now().isoformat(timespec="minutes")
                for h in nouveaux:
                    vus[h] = now

                if premiere_fois:
                    log(f"{nom} : 1er passage, {len(candidats)} liens mémorisés (aucun mail).")
                    continue
                if len(nouveaux) > MAX_NOUVEAUX:
                    log(f"{nom} : {len(nouveaux)} nouveaux liens d'un coup, probablement une refonte du site. "
                        f"Mémorisés sans mail ; ajoute un 'motif' dans config.yaml si ça se répète.")
                    continue
                log(f"{nom} : {len(candidats)} liens, {len(nouveaux)} nouveaux.")

                for url in nouveaux:
                    if not autorise(url):
                        continue
                    try:
                        titre, texte, _ = lire_page(browser, url)
                        time.sleep(PAUSE)
                        if USE_CLAUDE:
                            d = analyse_claude(regie, url, texte)
                            if not d.get("est_annonce_location_logement", True):
                                log(f"  pas une annonce : {url}")
                                continue
                        else:
                            d = analyse_sans_ia(texte)
                        if not correspond(d, texte, crit):
                            log(f"  hors critères : {url}")
                            continue
                        envoyer_mail(regie, url, titre or candidats[url] or url, d)
                        envoyes += 1
                        log(f"  ✉️  envoyé : {url}")
                    except Exception as e:
                        log(f"  erreur sur {url} : {e}")
            except Exception as e:
                log(f"{nom} : ERREUR {type(e).__name__}: {e}")
            finally:
                save_json(STATE_FILE, state)
        browser.close()

    save_json(DIAG_FILE, diag)
    log(f"Terminé : {envoyes} e-mail(s) envoyé(s).")


if __name__ == "__main__":
    sys.exit(main())
