#!/usr/bin/env python3
"""
Putzplan-Reminder Bot fuer Discourse.

Liest den Putzplan aus einem Discourse-Post (Markdown-Tabelle),
ermittelt die aktuelle und naechste Woche und postet eine Erinnerung.

Aufruf:
    python3 putzplan_reminder.py                # Post erstellen
    python3 putzplan_reminder.py --dry-run      # Nur anzeigen, nicht posten
    python3 putzplan_reminder.py --force         # Duplikat-Schutz umgehen

Cron (jeden Sonntag 19:00):
    0 19 * * 0 /usr/bin/python3 /opt/putzplan/putzplan_reminder.py

Umgebungsvariablen (oder .env-Datei):
    DISCOURSE_URL              - Forum-URL (z.B. https://forum.nullsieben.be)
    DISCOURSE_API_KEY          - API-Key des Bot-Users
    DISCOURSE_API_USERNAME     - Bot-Username (z.B. Putzbot)
    DISCOURSE_TOPIC_ID         - Topic-ID des Putzplan-Topics
    DISCOURSE_SCHEDULE_POST_ID - (optional) Post-ID mit der Tabelle
                                 Wenn nicht gesetzt, wird automatisch gesucht.
    DISCOURSE_MEMBER_GROUPS    - (optional) Kommagetrennte Gruppen-Namen
                                 z.B. anwaerter,mitglieder
    DISCOURSE_EXCLUDE_USERS    - (optional) Kommagetrennte Usernames
                                 die nie putzen muessen

Exit-Codes:
    0 - Erfolg (oder Duplikat erkannt)
    1 - Konfigurationsfehler
    2 - API-/Netzwerk-Fehler
    3 - Daten-Fehler (keine Wochen gefunden etc.)
"""

import argparse
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    print("Fehler: 'requests' nicht installiert. pip install requests")
    sys.exit(1)

# --- Konfiguration ---

# Datums-Format in der Tabelle: dd.mm.yyyy
DATE_FMT = "%d.%m.%Y"

# Retry-Konfiguration
API_RETRY_DELAY = 5   # Sekunden
API_MAX_RETRIES = 1   # 1x Retry bei Netzwerk-Fehlern
API_TIMEOUT = 30      # Request-Timeout in Sekunden

# Mindestanzahl Wochen fuer Auto-Erkennung eines Putzplan-Posts
MIN_WEEKS_FOR_DETECTION = 3


# --- API ---

class DiscourseAPI:
    def __init__(self, base_url, api_key, api_username):
        self.base_url = base_url.rstrip("/")
        self.api_username = api_username
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": api_key,
            "Api-Username": api_username,
            "Content-Type": "application/json",
        })

    def _request_with_retry(self, method, url, **kwargs):
        """HTTP-Request mit Retry bei Netzwerk-Fehlern und Rate-Limits."""
        last_error = None
        for attempt in range(API_MAX_RETRIES + 1):
            try:
                kwargs.setdefault("timeout", API_TIMEOUT)
                resp = self.session.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.ConnectionError as e:
                last_error = e
                if attempt < API_MAX_RETRIES:
                    print(f"  Verbindungsfehler, Retry in {API_RETRY_DELAY}s...")
                    time.sleep(API_RETRY_DELAY)
            except requests.Timeout as e:
                last_error = e
                if attempt < API_MAX_RETRIES:
                    print(f"  Timeout, Retry in {API_RETRY_DELAY}s...")
                    time.sleep(API_RETRY_DELAY)
            except requests.HTTPError as e:
                if resp.status_code == 429 and attempt < API_MAX_RETRIES:
                    retry_after = int(resp.headers.get("Retry-After", 10))
                    print(f"  Rate-Limit erreicht, warte {retry_after}s...")
                    time.sleep(retry_after)
                    last_error = e
                else:
                    raise
        raise last_error

    def get_post_raw(self, post_id):
        """Rohtext eines Posts laden."""
        resp = self._request_with_retry("GET", f"{self.base_url}/posts/{post_id}.json")
        return resp.json().get("raw", "")

    def get_post_cooked(self, topic_id, post_id):
        """Cooked-HTML eines Posts ueber die Topic-API laden.

        Nutzt /t/{topic_id}/posts.json statt /posts/{id}.json,
        da letzteres hoehere Berechtigungen erfordert.
        """
        posts = self.get_posts_batch(topic_id, [post_id])
        for post in posts:
            if post["id"] == post_id:
                return post.get("cooked", "")
        return ""

    def create_post(self, topic_id, raw):
        """Neuen Post in einem Topic erstellen."""
        resp = self._request_with_retry(
            "POST",
            f"{self.base_url}/posts.json",
            json={"topic_id": topic_id, "raw": raw},
        )
        return resp.json()

    def get_topic(self, topic_id):
        """Topic-Daten laden (inkl. Post-Stream)."""
        resp = self._request_with_retry("GET", f"{self.base_url}/t/{topic_id}.json")
        return resp.json()

    def get_recent_posts(self, topic_id, count=10):
        """Die letzten N Posts eines Topics laden."""
        topic = self.get_topic(topic_id)
        post_stream = topic.get("post_stream", {})
        all_post_ids = post_stream.get("stream", [])

        # Die letzten `count` Post-IDs
        recent_ids = all_post_ids[-count:]
        posts = []
        for post in post_stream.get("posts", []):
            if post["id"] in recent_ids:
                posts.append(post)

        # Fehlende Posts nachladen (Discourse liefert nur ~20 Posts inline)
        loaded_ids = {p["id"] for p in posts}
        missing_ids = [pid for pid in recent_ids if pid not in loaded_ids]
        if missing_ids:
            resp = self._request_with_retry(
                "GET",
                f"{self.base_url}/t/{topic_id}/posts.json",
                params={"post_ids[]": missing_ids},
            )
            for post in resp.json().get("post_stream", {}).get("posts", []):
                posts.append(post)

        return posts

    def get_topic_post_ids(self, topic_id):
        """Alle Post-IDs eines Topics laden (aus dem Post-Stream)."""
        topic = self.get_topic(topic_id)
        return topic.get("post_stream", {}).get("stream", [])

    def get_posts_batch(self, topic_id, post_ids):
        """Mehrere Posts eines Topics per Batch laden."""
        resp = self._request_with_retry(
            "GET",
            f"{self.base_url}/t/{topic_id}/posts.json",
            params={"post_ids[]": post_ids},
        )
        return resp.json().get("post_stream", {}).get("posts", [])

    def get_group_members(self, group_name):
        """Alle Usernames einer Discourse-Gruppe laden.

        Paginiert automatisch (Discourse liefert max 50 pro Seite).
        """
        members = []
        offset = 0
        limit = 50
        while True:
            resp = self._request_with_retry(
                "GET",
                f"{self.base_url}/groups/{group_name}/members.json",
                params={"offset": offset, "limit": limit},
            )
            data = resp.json()
            batch = data.get("members", [])
            if not batch:
                break
            for member in batch:
                members.append(member["username"])
            if len(batch) < limit:
                break
            offset += limit
        return members


# --- Tabellen-Parsing ---

def parse_schedule(raw_text):
    """Parst die Markdown-Tabelle und gibt eine Liste von Wochen zurueck.

    Jede Woche ist ein Dict:
        start_date: datetime.date
        end_date:   datetime.date
        putzer1:    str oder None
        putzer2:    str oder None
        raw_line:   Original-Zeile
    """
    weeks = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Split behaelt leere Zellen — Anfang/Ende-Pipes erzeugen leere Strings
        cells = [c.strip() for c in line.split("|")]
        # cells[0] und cells[-1] sind leer (vor erstem | und nach letztem |)
        # Wir brauchen cells[1], cells[2], cells[3] fuer Datum, Putzer1, Putzer2
        if len(cells) < 4:
            continue

        # Zweite Zelle (Index 1) muss ein Datumsbereich sein
        date_match = re.match(
            r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})",
            cells[1],
        )
        if not date_match:
            continue

        try:
            start = datetime.strptime(date_match.group(1), DATE_FMT).date()
            end = datetime.strptime(date_match.group(2), DATE_FMT).date()
        except ValueError:
            continue

        putzer1 = extract_name(cells[2]) if len(cells) > 2 else None
        putzer2 = extract_name(cells[3]) if len(cells) > 3 else None
        remarks_mentions = extract_all_mentions(cells[4]) if len(cells) > 4 else []

        weeks.append({
            "start_date": start,
            "end_date": end,
            "putzer1": putzer1,
            "putzer2": putzer2,
            "remarks_mentions": remarks_mentions,
            "raw_line": line,
        })

    return weeks


def parse_schedule_html(html):
    """Parst eine HTML-Tabelle (cooked) und gibt eine Liste von Wochen zurueck.

    Fallback fuer den Fall, dass raw-Markdown nicht verfuegbar ist
    (z.B. weil /posts/{id}.json 403 liefert).
    """
    weeks = []
    rows = re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        cells = re.findall(r'<t[dh]>(.*?)</t[dh]>', row, re.DOTALL)
        if len(cells) < 3:
            continue
        # HTML-Tags entfernen, nur Text behalten
        cell_texts = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        date_match = re.match(
            r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})",
            cell_texts[0],
        )
        if not date_match:
            continue

        try:
            start = datetime.strptime(date_match.group(1), DATE_FMT).date()
            end = datetime.strptime(date_match.group(2), DATE_FMT).date()
        except ValueError:
            continue

        putzer1 = extract_name(cell_texts[1]) if len(cell_texts) > 1 else None
        putzer2 = extract_name(cell_texts[2]) if len(cell_texts) > 2 else None
        remarks_mentions = extract_all_mentions(cell_texts[3]) if len(cell_texts) > 3 else []

        weeks.append({
            "start_date": start,
            "end_date": end,
            "putzer1": putzer1,
            "putzer2": putzer2,
            "remarks_mentions": remarks_mentions,
            "raw_line": "",
        })

    return weeks


def extract_name(cell_text):
    """Extrahiert den @Username aus einer Zelle.

    Erkennt Formate wie:
        @username
        @username ✅️
        @username 🌞19.02.
        pumi ✅  (ohne @)
    Gibt den bereinigten Namen zurueck (mit @ wenn vorhanden),
    oder None wenn die Zelle leer ist.
    """
    text = cell_text.strip()
    if not text:
        return None

    # @mention extrahieren (erster @username in der Zelle)
    mention = re.search(r"@([\w.\-]+)", text)
    if mention:
        return f"@{mention.group(1)}"

    # Kein @: Ersten zusammenhaengenden Text nehmen (vor Emoji/Whitespace)
    name_match = re.match(r"([\w.\-]+)", text)
    if name_match:
        return name_match.group(1)

    return None


def extract_all_mentions(text):
    """Extrahiert alle @Usernames aus einem Text.

    Returns: Liste von Usernames (lowercase, ohne @-Prefix).
    """
    return [m.lower() for m in re.findall(r"@([\w.\-]+)", text)]


# --- Wochen-Logik ---

def find_week(weeks, target_date):
    """Findet die Woche die target_date enthaelt."""
    for w in weeks:
        if w["start_date"] <= target_date <= w["end_date"]:
            return w
    return None


def find_next_week(weeks, current_week):
    """Findet die Woche nach current_week."""
    if current_week is None:
        return None
    for i, w in enumerate(weeks):
        if w["start_date"] == current_week["start_date"]:
            if i + 1 < len(weeks):
                return weeks[i + 1]
    return None


def format_date_range(week):
    """Formatiert den Datumsbereich: dd.mm. - dd.mm.yyyy"""
    start = week["start_date"]
    end = week["end_date"]
    return f"{start.strftime('%d.%m.')} - {end.strftime('%d.%m.%Y')}"


def get_names(week):
    """Gibt die Liste der eingetragenen Putzer zurueck."""
    names = []
    if week["putzer1"]:
        names.append(week["putzer1"])
    if week["putzer2"]:
        names.append(week["putzer2"])
    return names


# --- Gruppen-Abgleich ---

def count_assignments(weeks):
    """Zaehlt wie oft jede Person im Putzplan vorkommt.

    Beruecksichtigt alle Spalten: Putzer1, Putzer2, Bemerkungen.
    Jede @Mention zaehlt als ein Eintrag.

    Returns: Counter {username_lowercase: anzahl}
    """
    counts = Counter()
    for week in weeks:
        for name in (week["putzer1"], week["putzer2"]):
            if name:
                counts[name.lstrip("@").lower()] += 1
        for mention in week.get("remarks_mentions", []):
            counts[mention] += 1
    return counts


def load_group_members(api, group_names, exclude_users=None):
    """Laedt alle Mitglieder der angegebenen Gruppen.

    Args:
        exclude_users: Set von Usernames die ausgenommen werden

    Returns: Set von lowercase Usernames
    """
    if exclude_users is None:
        exclude_users = set()

    all_members = set()
    for group_name in group_names:
        try:
            members = api.get_group_members(group_name.strip())
            all_members.update(m.lower() for m in members)
            print(f"  Gruppe '{group_name}': {len(members)} Mitglieder")
        except requests.RequestException as e:
            print(f"  WARNUNG: Gruppe '{group_name}' konnte nicht geladen werden: {e}")

    return all_members - exclude_users


# --- Post-Text generieren ---

def build_reminder(this_week, next_week, unassigned=None, single_entry=None,
                   schedule_url=None):
    """Erstellt den Reminder-Text.

    Args:
        unassigned:   Liste von @usernames ohne Eintrag (nur wenn Dienst < 2)
        single_entry: Liste von @usernames mit nur 1 Eintrag (wenn Dienst < 2
                      aber alle mind. 1x eingetragen)
        schedule_url: URL zum Putzplan-Post
    """
    parts = []

    if this_week is None:
        parts.append(
            ":warning: Fuer diese Woche gibt es keinen Eintrag im Putzplan!"
        )
        if schedule_url:
            parts.append(f"[Link zum Putzplan]({schedule_url})")
        return "\n\n".join(parts)

    this_names = get_names(this_week)
    this_range = format_date_range(this_week)

    if len(this_names) == 2:
        parts.append(
            f":broom: **Kommende Woche {this_range}** haben "
            f"{this_names[0]} und {this_names[1]} Putzdienst."
        )
    elif len(this_names) == 1:
        parts.append(
            f":broom: **Kommende Woche {this_range}** hat "
            f"{this_names[0]} Putzdienst und kann noch "
            f"unterstützt werden — wer trägt sich noch ein?"
        )
    else:
        parts.append(
            f":warning: **Kommende Woche {this_range}** ist noch niemand "
            f"eingetragen — bitte schnappt euch die Dienste!"
        )

    if next_week:
        next_names = get_names(next_week)
        next_range = format_date_range(next_week)
        if len(next_names) == 2:
            parts.append(
                f"Die Woche darauf ({next_range}) haben "
                f"{next_names[0]} und {next_names[1]} Putzdienst."
            )
        elif len(next_names) == 1:
            parts.append(
                f"Die Woche darauf ({next_range}) hat "
                f"{next_names[0]} Putzdienst und kann noch "
                f"unterstützt werden — wer trägt sich noch ein?"
            )
        else:
            parts.append(
                f"Die Woche darauf ({next_range}) ist noch niemand "
                f"eingetragen — bitte schnappt euch die Dienste!"
            )

    if unassigned:
        names_str = ", ".join(unassigned)
        parts.append(
            f":mega: Noch nicht eingetragen haben sich bisher: {names_str}"
        )
    elif single_entry:
        names_str = ", ".join(single_entry)
        parts.append(
            f":point_right: Erst einmal eingetragen bisher: {names_str}"
        )

    if schedule_url:
        parts.append(f"[Link zum Putzplan]({schedule_url})")

    return "\n\n".join(parts)


# --- Duplikat-Schutz ---

def check_duplicate(api, topic_id, username, date_range_str):
    """Prueft ob der Bot bereits einen Reminder fuer diese Woche gepostet hat.

    Gibt True zurueck wenn ein Duplikat gefunden wurde.
    """
    try:
        posts = api.get_recent_posts(topic_id, count=10)
    except (requests.RequestException, KeyError):
        # Bei Fehler: kein Duplikat annehmen (lieber doppelt als gar nicht)
        print("  WARNUNG: Duplikat-Pruefung fehlgeschlagen, fahre fort.")
        return False

    for post in posts:
        post_username = post.get("username", "")
        post_raw = post.get("raw", "") or post.get("cooked", "")
        if post_username == username and date_range_str in post_raw:
            post_num = post.get("post_number", "?")
            print(f"  Duplikat gefunden: Post #{post_num} von {username} "
                  f"enthaelt bereits '{date_range_str}'")
            return True

    return False


# --- Auto-Erkennung Putzplan-Post ---

def find_schedule_post(api, topic_id):
    """Sucht automatisch den Post mit der Putzplan-Tabelle.

    Geht die Posts von hinten (neueste) nach vorne durch.
    Laedt Posts in Batches von 20 um Rate-Limits zu vermeiden.
    Nutzt raw-Markdown wenn verfuegbar, sonst cooked-HTML.

    Returns: (post_id, post_number) oder (None, None)
    """
    print("  Suche Putzplan-Tabelle automatisch...")
    all_post_ids = api.get_topic_post_ids(topic_id)

    if not all_post_ids:
        return None, None

    # Von hinten nach vorne suchen (neueste zuerst), Batches von 20
    batch_size = 20
    for batch_start in range(len(all_post_ids) - 1, -1, -batch_size):
        batch_end = max(0, batch_start - batch_size + 1)
        batch_ids = all_post_ids[batch_end:batch_start + 1]

        try:
            posts = api.get_posts_batch(topic_id, batch_ids)
        except requests.RequestException:
            continue

        # Innerhalb des Batches: neueste zuerst pruefen
        posts_by_id = {p["id"]: p for p in posts}
        for post_id in reversed(batch_ids):
            post = posts_by_id.get(post_id)
            if not post:
                continue
            # raw bevorzugen, sonst cooked-HTML parsen
            raw = post.get("raw", "")
            if raw:
                weeks = parse_schedule(raw)
            else:
                cooked = post.get("cooked", "")
                if "<table" not in cooked:
                    continue
                weeks = parse_schedule_html(cooked)
            if len(weeks) >= MIN_WEEKS_FOR_DETECTION:
                post_number = post.get("post_number")
                print(f"  Putzplan gefunden in Post-ID {post_id} "
                      f"(#{post_number}, {len(weeks)} Wochen)")
                return post_id, post_number

    return None, None


# --- Config ---

def load_env_files():
    """Laedt .env und config.env Dateien."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for filename in (".env", "config.env"):
        env_file = os.path.join(script_dir, filename)
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        v = v.strip().strip("\"'")
                        os.environ.setdefault(k.strip(), v)


def load_config():
    """Laedt und validiert die Konfiguration aus Umgebungsvariablen.

    Returns: dict mit url, key, username, topic_id, schedule_post_id (oder None)
    """
    load_env_files()

    url = os.environ.get("DISCOURSE_URL", "")
    key = os.environ.get("DISCOURSE_API_KEY", "")
    username = os.environ.get("DISCOURSE_API_USERNAME", "system")

    if not url or not key:
        print("FEHLER: DISCOURSE_URL und DISCOURSE_API_KEY muessen gesetzt sein!")
        print("  Entweder als Umgebungsvariable oder in .env / config.env")
        sys.exit(1)

    topic_id_str = os.environ.get("DISCOURSE_TOPIC_ID", "")
    if not topic_id_str:
        print("FEHLER: DISCOURSE_TOPIC_ID muss gesetzt sein!")
        print("  Entweder als Umgebungsvariable oder in .env / config.env")
        sys.exit(1)

    try:
        topic_id = int(topic_id_str)
    except ValueError:
        print(f"FEHLER: DISCOURSE_TOPIC_ID ist keine gueltige Zahl: '{topic_id_str}'")
        sys.exit(1)

    schedule_post_id = None
    schedule_post_id_str = os.environ.get("DISCOURSE_SCHEDULE_POST_ID", "")
    if schedule_post_id_str:
        try:
            schedule_post_id = int(schedule_post_id_str)
        except ValueError:
            print(f"FEHLER: DISCOURSE_SCHEDULE_POST_ID ist keine gueltige Zahl: "
                  f"'{schedule_post_id_str}'")
            sys.exit(1)

    # Gruppen fuer den Abgleich (kommagetrennt, optional)
    member_groups_str = os.environ.get("DISCOURSE_MEMBER_GROUPS", "")
    member_groups = [g.strip() for g in member_groups_str.split(",") if g.strip()]

    # User die vom Putzplan ausgenommen sind (kommagetrennt, optional)
    exclude_str = os.environ.get("DISCOURSE_EXCLUDE_USERS", "")
    exclude_users = {u.strip().lower() for u in exclude_str.split(",") if u.strip()}

    return {
        "url": url,
        "key": key,
        "username": username,
        "topic_id": topic_id,
        "schedule_post_id": schedule_post_id,
        "member_groups": member_groups,
        "exclude_users": exclude_users,
    }


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Putzplan-Reminder Bot fuer Discourse")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur anzeigen, nicht posten")
    parser.add_argument("--force", action="store_true",
                        help="Duplikat-Schutz umgehen")
    parser.add_argument("--date", type=str, default=None,
                        help="Datum simulieren (dd.mm.yyyy), Standard: heute")
    args = parser.parse_args()

    # Config laden
    config = load_config()
    url = config["url"]
    username = config["username"]
    topic_id = config["topic_id"]
    schedule_post_id = config["schedule_post_id"]

    try:
        api = DiscourseAPI(url, config["key"], username)
    except Exception as e:
        print(f"FEHLER: API-Initialisierung fehlgeschlagen: {e}")
        sys.exit(2)

    # Datum bestimmen
    if args.date:
        try:
            today = datetime.strptime(args.date, DATE_FMT).date()
        except ValueError:
            print(f"FEHLER: Ungueltiges Datum '{args.date}' (Format: dd.mm.yyyy)")
            sys.exit(1)
    else:
        today = datetime.now().date()

    # "Kommende Woche" = der naechste Montag (= morgen wenn heute Sonntag)
    # Wochentag: 0=Montag, 6=Sonntag
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7  # Wenn heute Montag, dann naechsten Montag
    # Ausnahme: Wenn heute Sonntag (weekday=6), ist morgen Montag
    if today.weekday() == 6:
        days_until_monday = 1
    next_monday = today + timedelta(days=days_until_monday)

    print(f"Heute:           {today.strftime(DATE_FMT)} ({['Mo','Di','Mi','Do','Fr','Sa','So'][today.weekday()]})")
    print(f"Kommende Woche:  ab {next_monday.strftime(DATE_FMT)}")
    print(f"Forum:           {url}")
    print(f"User:            {username}")
    print(f"Topic:           {topic_id}")
    print()

    # Putzplan-Post finden (auto-detect oder konfiguriert)
    schedule_post_number = None
    if schedule_post_id:
        print(f"Lade Putzplan (Post-ID {schedule_post_id})...")
        # Post-Nummer fuer den Link ermitteln
        try:
            posts = api.get_posts_batch(topic_id, [schedule_post_id])
            for p in posts:
                if p["id"] == schedule_post_id:
                    schedule_post_number = p.get("post_number")
                    break
        except requests.RequestException:
            pass
    else:
        print("Kein DISCOURSE_SCHEDULE_POST_ID gesetzt, suche automatisch...")
        try:
            schedule_post_id, schedule_post_number = find_schedule_post(api, topic_id)
        except requests.RequestException as e:
            print(f"FEHLER: Konnte Topic nicht laden: {e}")
            sys.exit(2)
        if not schedule_post_id:
            print("FEHLER: Kein Post mit Putzplan-Tabelle gefunden!")
            sys.exit(3)

    # Link zum Putzplan-Post
    schedule_url = f"{url}/t/{topic_id}"
    if schedule_post_number:
        schedule_url += f"/{schedule_post_number}"

    # Putzplan laden (raw bevorzugen, cooked-HTML als Fallback)
    weeks = []
    try:
        raw = api.get_post_raw(schedule_post_id)
        weeks = parse_schedule(raw)
        print(f"  {len(weeks)} Wochen im Plan gefunden (raw).")
    except requests.RequestException:
        print(f"  raw-Endpoint nicht verfuegbar, nutze cooked-HTML...")
        try:
            cooked = api.get_post_cooked(topic_id, schedule_post_id)
            weeks = parse_schedule_html(cooked)
            print(f"  {len(weeks)} Wochen im Plan gefunden (cooked).")
        except requests.RequestException as e:
            print(f"FEHLER: Putzplan konnte nicht geladen werden: {e}")
            sys.exit(2)

    if not weeks:
        print("FEHLER: Keine Wochen in der Tabelle gefunden!")
        sys.exit(3)

    # Aktuelle und naechste Woche finden
    this_week = find_week(weeks, next_monday)
    next_week = find_next_week(weeks, this_week)

    if this_week:
        print(f"  Diese Woche:    {format_date_range(this_week)}")
        print(f"    Putzer 1: {this_week['putzer1'] or '(leer)'}")
        print(f"    Putzer 2: {this_week['putzer2'] or '(leer)'}")
    else:
        print(f"  WARNUNG: Keine Woche fuer {next_monday.strftime(DATE_FMT)} gefunden!")

    if next_week:
        print(f"  Naechste Woche: {format_date_range(next_week)}")
        print(f"    Putzer 1: {next_week['putzer1'] or '(leer)'}")
        print(f"    Putzer 2: {next_week['putzer2'] or '(leer)'}")

    # Gruppen-Abgleich
    unassigned = []
    single_entry = []
    member_groups = config["member_groups"]
    if member_groups:
        print(f"Lade Gruppen-Mitglieder ({', '.join(member_groups)})...")
        assignment_counts = count_assignments(weeks)
        assigned_names = set(assignment_counts.keys())
        print(f"  {len(assigned_names)} Personen im Putzplan eingetragen.")

        all_members = load_group_members(
            api, member_groups, exclude_users=config["exclude_users"],
        )

        # Pruefen ob ein Dienst nicht voll besetzt ist (< 2 Putzer)
        this_full = this_week is not None and len(get_names(this_week)) >= 2
        next_full = next_week is None or len(get_names(next_week)) >= 2
        needs_filling = not this_full or not next_full

        if needs_filling:
            # Platz frei: gibt es Leute mit 0 Eintraegen?
            unassigned = sorted(
                f"@{name}" for name in (all_members - assigned_names)
            )
            if unassigned:
                print(f"  {len(unassigned)} Mitglieder noch ohne Putzdienst.")
            else:
                # Alle haben mind. 1x, zeige wer nur 1x eingetragen ist
                single_entry = sorted(
                    f"@{name}" for name in all_members
                    if assignment_counts.get(name, 0) == 1
                )
                if single_entry:
                    print(f"  {len(single_entry)} Mitglieder erst einmal eingetragen.")

    # Reminder-Text
    reminder = build_reminder(
        this_week, next_week,
        unassigned=unassigned, single_entry=single_entry,
        schedule_url=schedule_url,
    )
    print(f"\n--- Reminder-Text ---\n{reminder}\n--- Ende ---\n")

    if args.dry_run:
        print("[DRY-RUN] Post wird NICHT erstellt.")
        return

    # Duplikat-Schutz
    # Suche nach "Kommende Woche {range}" statt nur dem Datumsbereich,
    # da der Bereich auch im vorherigen Reminder als "Woche darauf" vorkommt.
    if not args.force:
        if this_week:
            dup_search_str = f"Kommende Woche {format_date_range(this_week)}"
        else:
            dup_search_str = "keinen Eintrag im Putzplan"
        print("Pruefe auf Duplikate...")
        if check_duplicate(api, topic_id, username, dup_search_str):
            print("ABBRUCH: Reminder fuer diese Woche wurde bereits gepostet.")
            print("  Verwende --force um trotzdem zu posten.")
            return

    # Posten
    print("Erstelle Post...")
    try:
        result = api.create_post(topic_id, reminder)
    except requests.HTTPError as e:
        print(f"FEHLER: Post konnte nicht erstellt werden: {e}")
        sys.exit(2)
    except requests.RequestException as e:
        print(f"FEHLER: Netzwerkfehler beim Posten: {e}")
        sys.exit(2)

    post_num = result.get("post_number", "?")
    post_id = result.get("id", "?")
    print(f"  OK — Post #{post_num} erstellt (ID: {post_id})")
    print(f"  {url}/t/{topic_id}/{post_num}")


if __name__ == "__main__":
    main()
