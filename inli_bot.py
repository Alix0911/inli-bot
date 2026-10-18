#!/usr/bin/env python3
"""
Bot inli : surveille une ou plusieurs recherches sur inli.fr et prévient
(notification push via ntfy et/ou e-mail) dès qu'une nouvelle annonce apparaît.

Usage :
    python inli_bot.py               # une vérification (pour cron / GitHub Actions)
    python inli_bot.py --loop 15     # tourne en continu, vérifie toutes les 15 min
    python inli_bot.py --test-notif  # envoie une notification de test

Configuration par variables d'environnement (voir README.md) :
    INLI_SEARCH_URLS  une ou plusieurs URL de recherche inli, séparées par des espaces
    NTFY_TOPIC        nom du canal ntfy (notif push sur le téléphone)
    NTFY_SERVER       serveur ntfy (défaut : https://ntfy.sh)
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO   pour l'e-mail
    INLI_STATE_FILE   fichier qui mémorise les annonces déjà vues (défaut : seen.json)
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Tes recherches : fais ta recherche sur inli.fr, copie l'URL de la page de résultats
# et ajoute-la ici entre guillemets, suivie d'une virgule.
SEARCHES = [
    "https://www.inli.fr/locations/offres/92_92?price_max=800",
    "https://www.inli.fr/locations/offres/val-de-marne-departement_d:94?price_max=800",
    "https://www.inli.fr/locations/offres/paris-departement_d:75?price_max=800",
]

# La variable INLI_SEARCH_URLS, si elle est définie, remplace la liste ci-dessus.
SEARCH_URLS = os.environ.get("INLI_SEARCH_URLS", "").split() or SEARCHES
DEFAULT_SEARCH = SEARCHES[0]  # recherche des anciens fichiers seen.json
STATE_FILE = Path(os.environ.get("INLI_STATE_FILE") or "seen.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip() or SMTP_USER

MAX_PAGES = 10            # sécurité : pages de résultats lues au maximum par recherche
PAUSE_BETWEEN_PAGES = 2   # secondes, pour rester discret avec le site
FORGET_AFTER_DAYS = 90    # on oublie une annonce disparue depuis plus de 90 jours

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Lien d'une annonce : /location-appartement-courbevoie-92400/PRV-316110
LISTING_PATH_RE = re.compile(
    r"^/location-([a-z]+)-([a-z0-9-]+)-(\d{5})/([A-Za-z0-9_-]+)/?$"
)
PRICE_RE = re.compile(r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)\s*€")
ROOMS_RE = re.compile(r"(Studio|\d+\s*pi[eè]ces?)", re.IGNORECASE)
SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
COUNT_RE = re.compile(r"(\d+)\s+logements?\b", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Lecture du site
# --------------------------------------------------------------------------- #

def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def with_page(url: str, page: int) -> str:
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def parse_listings(html: str, base_url: str) -> dict:
    """Renvoie {id_annonce: infos} pour toutes les annonces trouvées dans la page."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        match = LISTING_PATH_RE.match(urlparse(url).path)
        if not match:
            continue
        kind, city_slug, postcode, listing_id = match.groups()
        text = " ".join(a.get_text(" ", strip=True).split())

        # Une même annonce peut avoir plusieurs liens (photo + texte) : on garde le plus riche.
        if listing_id in found and len(found[listing_id]["raw"]) >= len(text):
            continue

        price = PRICE_RE.search(text)
        rooms = ROOMS_RE.search(text)
        surface = SURFACE_RE.search(text)
        found[listing_id] = {
            "id": listing_id,
            "url": url,
            "type": kind,
            "city": city_slug.replace("-", " ").title(),
            "postcode": postcode,
            "price": re.sub(r"\s", " ", price.group(1)) + " €" if price else "?",
            "rooms": rooms.group(1) if rooms else "?",
            "surface": surface.group(1).replace(",", ".") + " m²" if surface else "?",
            "raw": text,
        }
    return found


def page_count(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            pages.append(int(m.group(1)))
    return min(max(pages), MAX_PAGES)


def announced_count(html: str):
    m = COUNT_RE.search(BeautifulSoup(html, "html.parser").get_text(" "))
    return int(m.group(1)) if m else None


def scrape_search(search_url: str):
    """Lit toutes les pages d'une recherche. Renvoie (annonces, nombre annoncé par le site)."""
    first = fetch(search_url)
    listings = parse_listings(first, search_url)
    expected = announced_count(first)
    for page in range(2, page_count(first) + 1):
        time.sleep(PAUSE_BETWEEN_PAGES)
        page_url = with_page(search_url, page)
        listings.update(parse_listings(fetch(page_url), page_url))
    return listings, expected


# --------------------------------------------------------------------------- #
# Mémoire des annonces déjà vues
# --------------------------------------------------------------------------- #

def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"Fichier d'état illisible ({exc}), on repart de zéro.")
        return None


def save_state(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=FORGET_AFTER_DAYS)
    state["seen"] = {
        lid: info for lid, info in state["seen"].items()
        if datetime.fromisoformat(info["last_seen"]) >= cutoff
    }
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def describe(it: dict) -> str:
    return f"{it['city']} ({it['postcode']}) · {it['price']} cc · {it['rooms']} · {it['surface']}"


def notify_ntfy(title: str, message: str, click_url: str = None, priority: int = 4) -> None:
    if not NTFY_TOPIC:
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "tags": ["house"], "priority": priority}
    if click_url:
        payload["click"] = click_url
        payload["actions"] = [{"action": "view", "label": "Voir l'annonce", "url": click_url}]
    try:
        requests.post(NTFY_SERVER, json=payload, timeout=15).raise_for_status()
    except requests.RequestException as exc:
        log(f"Échec de la notification ntfy : {exc}")


def notify_email(subject: str, body: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and EMAIL_TO):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls(context=context)
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        log(f"Échec de l'envoi de l'e-mail : {exc}")


def send_baseline(added_searches: list, current: list) -> None:
    """Récapitulatif unique quand une recherche commence à être surveillée."""
    lines = [f"• {describe(it)}\n  {it['url']}" for it in current]
    title = f"Bot inli actif : {len(current)} annonce(s) en ce moment"
    intro = "Le bot surveille désormais :\n" + "\n".join(added_searches)
    notify_ntfy(title, "\n".join(describe(it) for it in current) or "Aucune annonce pour l'instant.",
                priority=3)
    notify_email(title, intro + "\n\nAnnonces actuelles :\n\n"
                 + ("\n\n".join(lines) or "Aucune pour l'instant."))


def send_new_listings(new: list) -> None:
    lines = [f"• {describe(it)}\n  {it['url']}" for it in new]
    for it in new:
        notify_ntfy(f"🏠 Nouveau logement inli : {it['price']}", describe(it), it["url"])
    notify_email(f"inli : {len(new)} nouvelle(s) annonce(s)", "\n\n".join(lines))


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #

def run_once() -> bool:
    """Une vérification complète. Renvoie False si toutes les recherches ont échoué."""
    state = load_state()
    if state is None:
        state = {"seen": {}, "broken_warned": False, "searches": []}
    elif "searches" not in state:
        # Ancien fichier d'état : il ne surveillait que la recherche par défaut.
        state["searches"] = [DEFAULT_SEARCH]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Une recherche surveillée pour la première fois ne déclenche pas une notif
    # par annonce déjà en ligne : on envoie un seul récapitulatif.
    new, baseline, added_searches = [], [], []
    ok, broken = 0, False
    for search_url in SEARCH_URLS:
        try:
            listings, expected = scrape_search(search_url)
        except requests.RequestException as exc:
            log(f"Erreur en lisant {search_url} : {exc}")
            continue
        ok += 1
        log(f"{search_url} → {len(listings)} annonce(s) lue(s), {expected} annoncée(s) par le site")
        if expected and not listings:
            broken = True
        is_new_search = search_url not in state["searches"]
        if is_new_search:
            state["searches"].append(search_url)
            added_searches.append(search_url)
        for lid, it in listings.items():
            if lid in state["seen"]:
                state["seen"][lid]["last_seen"] = now
            else:
                state["seen"][lid] = {"first_seen": now, "last_seen": now,
                                      "summary": describe(it), "url": it["url"]}
                (baseline if is_new_search else new).append(it)

    # Le site affiche des logements mais on n'en lit aucun : sa structure a sûrement changé.
    if broken and not state.get("broken_warned"):
        msg = ("Le site indique des logements mais le bot n'arrive plus à les lire. "
               "La structure de la page a probablement changé : le script doit être adapté.")
        notify_ntfy("⚠️ Bot inli à vérifier", msg, priority=3)
        notify_email("Bot inli à vérifier", msg)
    state["broken_warned"] = broken

    if added_searches:
        log(f"{len(added_searches)} recherche(s) ajoutée(s), {len(baseline)} annonce(s) déjà en ligne")
        send_baseline(added_searches, baseline)
    if new:
        log(f"{len(new)} nouvelle(s) annonce(s)")
        send_new_listings(new)
    elif not added_searches:
        log("Rien de nouveau.")

    save_state(state)
    return ok > 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Surveille des recherches inli.fr")
    parser.add_argument("--loop", type=float, metavar="MINUTES",
                        help="tourner en continu avec une vérification toutes les N minutes")
    parser.add_argument("--test-notif", action="store_true",
                        help="envoyer une notification et un e-mail de test, puis quitter")
    args = parser.parse_args()

    if args.test_notif:
        notify_ntfy("Test bot inli", "Si tu lis ceci, les notifications fonctionnent 👍",
                    "https://www.inli.fr/")
        notify_email("Test bot inli", "Si tu lis ceci, l'envoi d'e-mails fonctionne.")
        log("Notification de test envoyée (si ntfy / SMTP sont configurés).")
        return

    if not NTFY_TOPIC and not SMTP_HOST:
        log("Attention : ni NTFY_TOPIC ni SMTP_HOST configurés, aucune alerte ne sera envoyée.")

    if args.loop:
        interval = max(args.loop, 5) * 60  # pas plus d'une vérification toutes les 5 min
        while True:
            try:
                run_once()
            except Exception as exc:  # on ne veut pas que la boucle s'arrête
                log(f"Erreur inattendue : {exc}")
            time.sleep(interval)
    else:
        sys.exit(0 if run_once() else 1)


if __name__ == "__main__":
    main()