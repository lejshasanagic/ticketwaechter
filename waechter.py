"""Ticketwächter für Schweden - Bosnien (Nations League, 14.11.2026, Solna).

Läuft alle paar Minuten auf GitHub Actions und meldet per Mail über iCloud (und optional
per Push über ntfy), sobald es Neuigkeiten zu Tickets für den BiH-Block gibt.

Überwacht:
  - nfsbih.ba:        jeder NEUE Artikel wird geöffnet; Meldung bei JEDER Ticket-News
                      (lieber einmal zu viel als einmal zu wenig)
  - event.ba:         neue Events mit Schweden/Solna/Stockholm im Namen
  - bhff.fans:        Login mit deinem Konto, jede Änderung auf der Seite wird gemeldet
  - Strawberry Arena, SvFF-Ticketseite: jede Änderung zum Spiel (schwedischer Verkauf)
  - klix.ba, sport.ba, faktor.ba, reprezentacija.ba, meridiansport.ba:
                      neue Artikel über Schweden + Tickets/Fans

Alle Zugangsdaten kommen aus Umgebungsvariablen (GitHub Secrets), nie aus dem Code.
Wichtig: Das Repo ist öffentlich, die Logs auch. Darum wird nichts Persönliches
geloggt oder in zustand.json gespeichert, nur Links und ein Prüfwert (Hash).
"""

import hashlib
import html
import http.cookiejar
import json
import os
import re
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ZUSTAND_DATEI = Path(__file__).with_name("zustand.json")
FEHLER_BIS_WARNUNG = 6
LEBENSZEICHEN_AB_STUNDE = 8  # einmal täglich ab 8 Uhr: "Wächter läuft noch"

NFSBIH_LISTEN = [
    "https://www.nfsbih.ba/vijesti/",
    "https://www.nfsbih.ba/vijesti/nogomet-m/a-reprezentacija-m/",
]
EVENT_BA = "https://event.ba/"
BHFF = "https://bhff.fans"
# Schwedische Seiten: Name -> (Adresse, Ankertext, Zeilen ab Anker). Jede Änderung wird gemeldet.
SCHWEDISCHE_SEITEN = {
    "Strawberry Arena": ("https://strawberryarena.se/en/events/sport/sweden-bosnia-herzegovina/",
                         "About the event", 200),
    "SvFF Biljetter": ("https://www.svenskfotboll.se/biljett/", "Sverige–Bosnien-Hercegovina", 6),
}
# Portal: (Übersichtsseiten, Muster für Artikel-Links)
# faktor.ba, reprezentacija.ba und meridiansport.ba hatten früher schon Infos VOR dem Verband.
PORTALE = {
    "klix.ba": (["https://www.klix.ba/sport/nogomet"], r'href="(/sport/nogomet/[^"?#]+/\d+)"'),
    "sport.ba": (["https://sport.ba/", "https://sport.ba/kategorija/fudbal"], r'href="(/clanak/\d+/[^"?#]+)"'),
    "faktor.ba": (["https://faktor.ba/sport/fudbal"], r'href="(/sport/fudbal/[^"?#]+/\d+)"'),
    "reprezentacija.ba": (["https://reprezentacija.ba/"], r'href="(https://reprezentacija\.ba/\d+-[^"?#]+)"'),
    "meridiansport.ba": (["https://meridiansport.ba/", "https://meridiansport.ba/fudbal/reprezentativna-takmicenja/"],
                         r'href="(https://meridiansport\.ba/(?!author/|wp-)[^"?#]+/[a-z0-9-]{15,}/)"'),
}

# Bosnisch: Švedska / Švedskom / Švedske ... (mit Š!), dazu Solna/Stockholm
SCHWEDEN = re.compile(r"[sš]vedsk|solna|stockholm", re.I)
TICKETS = re.compile(r"ulaznic|navija|prodaj|gostuj|karte|bhff", re.I)
ULAZNICE = re.compile(r"ulaznic", re.I)  # "Tickets" - beim Verband wird jede Ticket-News gemeldet
EVENT_NAME = re.compile(r"[\w-]*(?:[sš]vedsk|solna|stockholm)[\w-]*", re.I)

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Ticketwaechter"}


# ---------- Hilfsfunktionen ----------

def jetzt():
    return datetime.now(ZoneInfo("Europe/Berlin"))


def log(nachricht):
    print(f"[{jetzt():%d.%m. %H:%M}] {nachricht}", flush=True)


def lade_seite(url, opener=None):
    anfrage = urllib.request.Request(url, headers=BROWSER)
    oeffnen = opener.open if opener else urllib.request.urlopen
    with oeffnen(anfrage, timeout=30) as antwort:
        return antwort.read().decode("utf-8", errors="replace")


def nur_text(seiten_html):
    seiten_html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", seiten_html, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", seiten_html))
    return "\n".join(zeile.strip() for zeile in text.splitlines() if zeile.strip())


# ---------- Benachrichtigung ----------

def sende_push(betreff, text, dringend):
    thema = os.environ.get("NTFY_TOPIC")
    if not thema:
        return False
    push = urllib.request.Request(
        f"https://ntfy.sh/{thema}",
        data=text.encode("utf-8"),
        headers={"Title": betreff.encode("utf-8"),
                 "Priority": "urgent" if dringend else "default",
                 "Tags": "soccer"},
    )
    urllib.request.urlopen(push, timeout=30)
    return True


def sende_mail(betreff, text, dringend):
    """Mail von deinem eigenen Postfach an dich selbst (landet so nicht im Spam).
    Standard ist iCloud; mit SMTP_SERVER=smtp.gmail.com geht auch Gmail."""
    absender = os.environ["MAIL_USER"]
    mail = EmailMessage()
    mail["From"] = absender
    mail["To"] = os.environ.get("MAIL_TO") or absender
    mail["Subject"] = betreff
    if dringend:
        mail["X-Priority"] = "1"
        mail["Importance"] = "high"
    mail.set_content(text)
    with smtplib.SMTP(os.environ.get("SMTP_SERVER") or "smtp.mail.me.com", 587, timeout=30) as smtp:
        smtp.starttls()
        # iCloud: manche Konten melden sich mit der Apple-ID an (MAIL_LOGIN), senden aber von @icloud.com
        smtp.login(os.environ.get("MAIL_LOGIN") or absender, os.environ["MAIL_APP_PASSWORD"])
        smtp.send_message(mail)


def melde(betreff, text, dringend=True):
    """Push und Mail unabhängig voneinander: fällt eins aus, kommt das andere trotzdem."""
    try:
        if sende_push(betreff, text, dringend):
            log(f"Push verschickt: {betreff}")
    except Exception as fehler:
        log(f"Push fehlgeschlagen: {fehler}")
    try:
        sende_mail(betreff, text, dringend)
        log(f"Mail verschickt: {betreff}")
    except Exception as fehler:
        log(f"Mail fehlgeschlagen: {fehler}")
        try:
            sende_push("Ticketwächter: Mail klappt nicht", f"Fehler beim Mailversand: {fehler}", False)
        except Exception:
            pass


# ---------- nfsbih.ba ----------

def artikel_links(listen_html):
    links = set()
    for pfad in re.findall(r'href="(/vijesti/[^"?#]+)"', listen_html):
        slug = pfad.rstrip("/").split("/")[-1]
        if slug.count("-") >= 3:  # Artikel haben lange Titel-Slugs, Kategorien nicht
            links.add("https://www.nfsbih.ba" + pfad)
    return links


def artikel_text(artikel_html):
    """Titel + eigentlicher Artikeltext (ohne Seitenleiste/Menü)."""
    titel = re.search(r"<title>(.*?)</title>", artikel_html, re.S)
    start = artikel_html.find('class="itemIntroText"')
    ende = artikel_html.find('class="mod-content', start)
    rumpf = artikel_html[start:ende] if start != -1 else ""
    return html.unescape((titel.group(1).strip() if titel else "") + "\n" + re.sub(r"<[^>]+>", " ", rumpf))


def pruefe_nfsbih(zustand, erster_lauf):
    funde = []
    for liste in NFSBIH_LISTEN:
        for link in sorted(artikel_links(lade_seite(liste)) - set(zustand["gesehen"])):
            zustand["gesehen"].append(link)
            if erster_lauf:
                continue
            log(f"Neuer Artikel: {link}")
            try:
                text = artikel_text(lade_seite(link))
            except Exception as fehler:  # lieber melden als verpassen
                funde.append(f"NFSBiH-Artikel (konnte nicht geöffnet werden: {fehler}):\n{link}")
                continue
            if ULAZNICE.search(text) or (SCHWEDEN.search(text) and TICKETS.search(text)):
                funde.append(f"NFSBiH-Artikel: {text.splitlines()[0]}\n{link}")
    return funde


# ---------- Nachrichtenportale (klix.ba, sport.ba, faktor.ba, ...) ----------

def pruefe_portal(name, zustand, erster_lauf):
    seiten, muster = PORTALE[name]
    basis = re.match(r"https?://[^/]+", seiten[0]).group(0)
    funde = []
    for seite in seiten:
        for pfad in sorted(set(re.findall(muster, lade_seite(seite))) - set(zustand["gesehen"])):
            zustand["gesehen"].append(pfad)
            # Nur Artikel öffnen, deren Titel nach Schweden oder Tickets klingt
            if erster_lauf or not (SCHWEDEN.search(pfad) or ULAZNICE.search(pfad)):
                continue
            link = pfad if pfad.startswith("http") else basis + pfad
            log(f"{name}: prüfe {link}")
            try:
                text = nur_text(lade_seite(link))
            except Exception as fehler:  # lieber melden als verpassen
                funde.append(f"{name}-Artikel (konnte nicht geöffnet werden: {fehler}):\n{link}")
                continue
            if SCHWEDEN.search(text) and TICKETS.search(text):
                funde.append(f"{name}-Artikel:\n{link}")
    return funde


# ---------- Schwedische Seiten (Gastgeber verkauft Gästeblock oft selbst) ----------

def pruefe_schwedische_seite(name, zustand, erster_lauf):
    url, anker, anzahl = SCHWEDISCHE_SEITEN[name]
    zeilen = nur_text(lade_seite(url)).splitlines()
    # Nur der Teil zum Spiel; fehlt der Anker, ganze Seite (lieber zu viel melden)
    start = zeilen.index(anker) if anker in zeilen else 0
    zeilen = zeilen[start:start + anzahl]
    alt = zustand.setdefault("seiten", {}).get(name, [])
    zustand["seiten"][name] = zeilen
    neu = [z for z in zeilen if z not in alt]
    if alt and neu and not erster_lauf:
        return [f"{name} hat sich geändert. Neu:\n" + "\n".join(neu[:40]) + f"\n\n{url}"]
    return []


# ---------- event.ba ----------

def pruefe_event_ba(zustand, erster_lauf):
    funde = []
    for name in sorted({n.lower() for n in EVENT_NAME.findall(html.unescape(lade_seite(EVENT_BA)))}):
        schluessel = "event.ba:" + name
        if schluessel not in zustand["gesehen"]:
            zustand["gesehen"].append(schluessel)
            if not erster_lauf:
                funde.append(f"Neues Event auf event.ba: {name}\nhttps://event.ba/{name}")
    return funde


# ---------- bhff.fans (mit Login) ----------

def bhff_einloggen():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login_seite = lade_seite(BHFF + "/index.php?option=com_users&view=login", opener)
    formular = re.search(r'<form action="([^"]*task=user\.login[^"]*)".*?</form>', login_seite, re.S)
    if not formular:
        raise RuntimeError("bhff.fans: Login-Formular nicht gefunden")
    felder = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', formular.group(0)))
    felder["username"] = os.environ["BHFF_USER"]
    felder["password"] = os.environ["BHFF_PASSWORD"]
    ziel = BHFF + html.unescape(formular.group(1))
    anfrage = urllib.request.Request(ziel, data=urllib.parse.urlencode(felder).encode(), headers=BROWSER)
    with opener.open(anfrage, timeout=30) as antwort:
        nach_login = antwort.read().decode("utf-8", errors="replace")
    if 'name="password"' in nach_login:
        raise RuntimeError("bhff.fans: Login fehlgeschlagen (Benutzername/Passwort prüfen)")
    return opener


def bhff_inhalt(seiten_html):
    """Nur der Hauptinhalt, ohne wechselnde Tokens, damit nicht jede Sekunde 'neu' ist."""
    haupt = re.search(r"<main.*?</main>", seiten_html, re.S | re.I)
    text = nur_text(haupt.group(0) if haupt else seiten_html)
    return re.sub(r"\b[0-9a-f]{32}\b", "", text)


def pruefe_bhff(zustand, erster_lauf):
    if not os.environ.get("BHFF_USER"):
        return []
    opener = bhff_einloggen()
    seite = os.environ.get("BHFF_SEITE") or "/bs/pocetna-bs"
    text = bhff_inhalt(lade_seite(BHFF + seite, opener))
    pruefwert = hashlib.sha256(text.encode()).hexdigest()
    alt = zustand.get("bhff_pruefwert")
    zustand["bhff_pruefwert"] = pruefwert
    log("bhff.fans: eingeloggt, Seite " + ("unverändert" if alt == pruefwert else "geprüft"))
    if alt and alt != pruefwert and not erster_lauf:
        # Inhalt nur in die (private) Mail, nie ins öffentliche Log
        return [f"bhff.fans hat sich geändert! Aktueller Inhalt:\n\n{text[:3000]}\n\n{BHFF}{seite}"]
    return []


# ---------- Ablauf ----------

def main():
    if "--test" in sys.argv:
        melde("Ticketwächter: Test", "Wenn du das liest, funktioniert die Benachrichtigung.", dringend=False)
        return

    zustand = json.loads(ZUSTAND_DATEI.read_text()) if ZUSTAND_DATEI.exists() else {}
    zustand.setdefault("gesehen", [])
    zustand.setdefault("fehler", {})
    erster_lauf = not zustand["gesehen"]

    funde = []
    pruefungen = [
        ("nfsbih.ba", pruefe_nfsbih),
        ("event.ba", pruefe_event_ba),
        ("bhff.fans", pruefe_bhff),
    ]
    for name in SCHWEDISCHE_SEITEN:
        pruefungen.append((name, lambda z, e, n=name: pruefe_schwedische_seite(n, z, e)))
    for name in PORTALE:
        pruefungen.append((name, lambda z, e, n=name: pruefe_portal(n, z, e)))
    for name, pruefung in pruefungen:
        try:
            funde += pruefung(zustand, erster_lauf)
            zustand["fehler"][name] = 0
        except Exception as fehler:
            anzahl = zustand["fehler"].get(name, 0) + 1
            zustand["fehler"][name] = anzahl
            log(f"Fehler bei {name} ({anzahl}x in Folge): {fehler}")
            if anzahl == FEHLER_BIS_WARNUNG:
                melde(f"Ticketwächter: Problem mit {name}",
                      f"{name} klappt seit {anzahl} Prüfungen nicht.\nLetzter Fehler: {fehler}", dringend=False)

    if funde:
        melde("TICKETS Schweden - Bosnien: Neue Info!",
              "\n\n---\n\n".join(funde) + "\n\nSchnell reinschauen, Tickets sind oft in Minuten weg!")
    elif erster_lauf:
        log(f"Erster Lauf: {len(zustand['gesehen'])} vorhandene Einträge gemerkt.")
        melde("Ticketwächter gestartet", "Der Wächter läuft ab jetzt rund um die Uhr.", dringend=False)
    else:
        log("Nichts Neues.")

    heute = jetzt().strftime("%Y-%m-%d")
    if zustand.get("lebenszeichen") != heute and jetzt().hour >= LEBENSZEICHEN_AB_STUNDE:
        if not erster_lauf:
            melde("Ticketwächter läuft noch", "Alles ok, bisher keine Tickets für Solna.", dringend=False)
        zustand["lebenszeichen"] = heute

    zustand["gesehen"].sort()
    ZUSTAND_DATEI.write_text(json.dumps(zustand, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
