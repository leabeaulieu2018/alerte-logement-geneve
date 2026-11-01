"""
Agent d'alerte logement Genève.

Toutes les X minutes :
  1. ouvre la page "location" (déjà filtrée) de chaque régie ;
  2. si la liste des annonces a changé, demande à Claude d'en extraire les annonces ;
  3. filtre selon tes critères (loyer, NPA, pièces, type) ;
  4. pour chaque NOUVELLE annonce, ouvre la fiche, extrait contact + visites ;
  5. t'envoie un e-mail par annonce.
"""

import hashlib
import html
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from urllib import robotparser
from urllib.parse import urlparse

import anthropic
import yaml
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "state.json")
CONFIG_FILE = os.path.join(ROOT, "config.yaml")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
USER_AGENT = "Mozilla/5.0 (compatible; AlerteLogementGeneve/1.0; usage personnel)"
MAX_CHARS = 30000          # texte envoyé à Claude par page
PAUSE_ENTRE_PAGES = 3      # secondes, pour rester poli avec les sites

client = anthropic.Anthropic()  # lit ANTHROPIC_API_KEY


# --------------------------------------------------------------------------- utilitaires

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


_robots_cache = {}


def autorise_par_robots(url):
    """Respecte le robots.txt de chaque site."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None  # robots.txt illisible : on considère autorisé
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def ask_claude_json(prompt, max_tokens=4000):
    msg = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system="Tu es un extracteur de données. Tu réponds UNIQUEMENT avec du JSON valide, "
               "sans texte autour ni balises markdown.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# --------------------------------------------------------------------------- navigation

def lire_page(browser, url):
    """Retourne (texte visible, liste de liens [texte, href]) d'une page, JavaScript inclus."""
    page = browser.new_page(user_agent=USER_AGENT, locale="fr-CH")
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        # fait défiler pour charger les annonces en "lazy loading"
        for _ in range(4):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
        texte = page.inner_text("body")
        liens = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => [(e.innerText || '').trim().slice(0, 150), e.href])",
        )
        return texte, liens
    finally:
        page.close()


# --------------------------------------------------------------------------- extraction

def extraire_annonces(regie, texte, liens):
    domaine = urlparse(regie["url"]).netloc
    liens_utiles = [l for l in liens if domaine in l[1] and l[1].startswith("http")]
    liste_liens = "\n".join(f"- {t} -> {h}" for t, h in liens_utiles[:400])

    prompt = f"""Voici le contenu d'une page de résultats de location de la régie « {regie['nom']} » à Genève.

TEXTE DE LA PAGE :
{texte[:MAX_CHARS]}

LIENS DE LA PAGE :
{liste_liens}

Retourne un tableau JSON des annonces de LOCATION de logements présentes sur la page.
Ignore parkings, boxes, bureaux, arcades, dépôts et annonces de vente.
Chaque élément :
{{
  "url": "lien vers la fiche, choisi parmi les liens fournis",
  "titre": "string",
  "adresse": "string ou null",
  "npa": "code postal à 4 chiffres en string, ou null",
  "commune": "string ou null",
  "type": "appartement" | "maison" | "studio" | "autre",
  "pieces": nombre ou null,
  "loyer_chf": nombre (loyer mensuel charges comprises si possible) ou null,
  "surface_m2": nombre ou null,
  "disponibilite": "string ou null"
}}
S'il n'y a aucune annonce, retourne []."""
    return ask_claude_json(prompt)


def extraire_details(regie, url, texte):
    prompt = f"""Voici le texte d'une fiche d'annonce de location de la régie « {regie['nom']} » à Genève ({url}).

{texte[:MAX_CHARS]}

Retourne un objet JSON :
{{
  "adresse": "string ou null",
  "pieces": nombre ou null,
  "etage": "string ou null",
  "surface_m2": nombre ou null,
  "loyer_net_chf": nombre ou null,
  "charges_chf": nombre ou null,
  "loyer_total_chf": nombre ou null,
  "disponibilite": "string ou null",
  "contact": {{"nom": "string ou null", "telephone": "string ou null", "email": "string ou null"}},
  "visites": [{{"date": "string", "heure": "string ou null", "details": "string ou null"}}],
  "comment_postuler": "string ou null (formulaire en ligne, e-mail, inscription à la visite, documents demandés...)",
  "resume": "2 phrases max sur le logement"
}}
N'invente rien : mets null si l'information n'est pas dans le texte."""
    return ask_claude_json(prompt, max_tokens=2000)


# --------------------------------------------------------------------------- filtre

def correspond(annonce, c):
    loyer = annonce.get("loyer_chf")
    if loyer is not None:
        if c.get("loyer_min") and loyer < c["loyer_min"]:
            return False
        if c.get("loyer_max") and loyer > c["loyer_max"]:
            return False
    npa = (annonce.get("npa") or "").strip()
    if c.get("npa") and npa and npa not in [str(n) for n in c["npa"]]:
        return False
    pieces = annonce.get("pieces")
    if pieces is not None:
        if c.get("pieces_min") and pieces < c["pieces_min"]:
            return False
        if c.get("pieces_max") and pieces > c["pieces_max"]:
            return False
    if c.get("types") and annonce.get("type") not in c["types"]:
        return False
    return True


# --------------------------------------------------------------------------- e-mail

def envoyer_mail(regie, annonce, d):
    def v(x, defaut="—"):
        return html.escape(str(x)) if x not in (None, "", []) else defaut

    loyer = d.get("loyer_total_chf") or annonce.get("loyer_chf")
    pieces = d.get("pieces") or annonce.get("pieces")
    adresse = d.get("adresse") or annonce.get("adresse") or annonce.get("titre")

    visites = d.get("visites") or []
    visites_html = "".join(
        f"<li><b>{v(x.get('date'))}</b> {v(x.get('heure'), '')} {v(x.get('details'), '')}</li>"
        for x in visites
    ) or "<li>Aucune visite indiquée dans l'annonce</li>"

    contact = d.get("contact") or {}
    corps = f"""
<div style="font-family:Arial,sans-serif;font-size:14px;max-width:600px">
  <h2 style="margin:0 0 8px">{v(pieces)} pièces · {v(loyer)} CHF/mois</h2>
  <p style="margin:0 0 12px;color:#555">{v(adresse)} · <b>{v(regie['nom'])}</b></p>
  <p><a href="{html.escape(annonce['url'])}" style="background:#1a56db;color:#fff;padding:10px 16px;
     border-radius:6px;text-decoration:none">Ouvrir l'annonce</a></p>
  <h3>Visites</h3><ul>{visites_html}</ul>
  <h3>Contact</h3>
  <p>{v(contact.get('nom'))}<br>📞 {v(contact.get('telephone'))}<br>✉️ {v(contact.get('email'))}</p>
  <h3>Comment postuler</h3><p>{v(d.get('comment_postuler'))}</p>
  <h3>Détails</h3>
  <p>Loyer net : {v(d.get('loyer_net_chf'))} CHF · Charges : {v(d.get('charges_chf'))} CHF<br>
     Surface : {v(d.get('surface_m2') or annonce.get('surface_m2'))} m² · Étage : {v(d.get('etage'))}<br>
     Disponibilité : {v(d.get('disponibilite') or annonce.get('disponibilite'))}</p>
  <p style="color:#555">{v(d.get('resume'), '')}</p>
  <p style="color:#999;font-size:12px">Informations extraites automatiquement : vérifie sur l'annonce.</p>
</div>"""

    msg = EmailMessage()
    msg["Subject"] = f"🏠 {pieces or '?'} p. · {loyer or '?'} CHF · {adresse} ({regie['nom']})"
    msg["From"] = os.environ.get("MAIL_FROM", os.environ["SMTP_USER"])
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(f"Nouvelle annonce : {annonce['url']}")
    msg.add_alternative(corps, subtype="html")

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)


# --------------------------------------------------------------------------- boucle principale

def main():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    criteres = config["criteres"]
    regies = [r for r in config["regies"] if r.get("url")]
    if not regies:
        log("Aucune URL de régie renseignée dans config.yaml.")
        return

    state = load_json(STATE_FILE, {"vues": {}, "empreintes": {}, "initialisees": []})
    envoyes = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for regie in regies:
            nom, url = regie["nom"], regie["url"]
            try:
                if not autorise_par_robots(url):
                    log(f"{nom} : robots.txt interdit cette page, ignorée.")
                    continue

                texte, liens = lire_page(browser, url)
                time.sleep(PAUSE_ENTRE_PAGES)

                # Empreinte = ensemble des liens de la page : si rien n'a bougé, on n'appelle pas Claude.
                empreinte = hashlib.sha256(
                    "\n".join(sorted({h for _, h in liens})).encode()
                ).hexdigest()
                if state["empreintes"].get(nom) == empreinte:
                    log(f"{nom} : inchangé.")
                    continue
                state["empreintes"][nom] = empreinte

                annonces = extraire_annonces(regie, texte, liens)
                premiere_fois = nom not in state["initialisees"]
                nouvelles = [a for a in annonces if a.get("url") and a["url"] not in state["vues"]]
                log(f"{nom} : {len(annonces)} annonces, {len(nouvelles)} nouvelles.")

                for a in nouvelles:
                    state["vues"][a["url"]] = datetime.now().isoformat(timespec="minutes")
                    if premiere_fois and not config.get("envoyer_au_premier_passage", False):
                        continue  # 1er passage : on mémorise l'existant sans t'inonder de mails
                    if not correspond(a, criteres):
                        continue
                    details = {}
                    if autorise_par_robots(a["url"]):
                        try:
                            t, _ = lire_page(browser, a["url"])
                            details = extraire_details(regie, a["url"], t)
                            time.sleep(PAUSE_ENTRE_PAGES)
                        except Exception as e:
                            log(f"  détails indisponibles ({e}), envoi quand même.")
                    if not correspond({**a, **{k: v for k, v in {
                        "loyer_chf": details.get("loyer_total_chf"),
                        "pieces": details.get("pieces")}.items() if v is not None}}, criteres):
                        continue
                    envoyer_mail(regie, a, details)
                    envoyes += 1
                    log(f"  ✉️  envoyé : {a.get('titre') or a['url']}")

                if premiere_fois:
                    state["initialisees"].append(nom)

            except Exception as e:
                log(f"{nom} : ERREUR {type(e).__name__}: {e}")
            finally:
                save_json(STATE_FILE, state)  # sauvegarde après chaque régie

        browser.close()

    log(f"Terminé : {envoyes} e-mail(s) envoyé(s).")


if __name__ == "__main__":
    sys.exit(main())
