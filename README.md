# Putzplan-Reminder für Discourse

Ein Python-Bot, der wöchentlich einen Putzplan-Reminder in ein Discourse-Forum postet. Er liest eine Markdown-Tabelle aus einem konfigurierten Topic, ermittelt die kommende und übernächste Woche und erstellt automatisch einen Erinnerungs-Post.

## Features

- **Automatische Tabellen-Erkennung** — findet den neuesten Post mit gültiger Putzplan-Tabelle im Topic
- **Gruppen-Abgleich** — prüft gegen Discourse-Gruppen, wer noch keinen Putzdienst eingetragen hat
- **Duplikat-Schutz** — verhindert doppelte Reminder für dieselbe Woche
- **Retry-Logik** — automatische Wiederholung bei Netzwerkfehlern und Rate-Limits

## Voraussetzungen

- Python 3.7+
- `requests` (`pip install requests`)
- Ein Discourse-Forum mit API-Key

## Installation

```bash
git clone https://github.com/elRicharde/putzplan_reminder_for_discourse.git
cd putzplan_reminder_for_discourse
pip install requests
cp .env.example .env
# .env mit eigenen Werten befüllen
```

## Konfiguration

Kopiere `.env.example` nach `.env` und trage die Werte ein:

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `DISCOURSE_URL` | Ja | Forum-URL (z.B. `https://forum.example.com`) |
| `DISCOURSE_API_KEY` | Ja | API-Key des Bot-Users |
| `DISCOURSE_API_USERNAME` | Ja | Bot-Username (z.B. `Putzbot`) |
| `DISCOURSE_TOPIC_ID` | Ja | Topic-ID des Putzplan-Topics |
| `DISCOURSE_SCHEDULE_POST_ID` | Nein | Post-ID mit der Tabelle (sonst Auto-Erkennung) |
| `DISCOURSE_MEMBER_GROUPS` | Nein | Kommagetrennte Gruppen-Namen für den Abgleich |
| `DISCOURSE_EXCLUDE_USERS` | Nein | Kommagetrennte Usernames, die nie putzen müssen |

## Verwendung

```bash
# Reminder erstellen
python putzplan_reminder.py

# Nur anzeigen, nicht posten
python putzplan_reminder.py --dry-run

# Duplikat-Schutz umgehen
python putzplan_reminder.py --force

# Datum simulieren
python putzplan_reminder.py --date 15.06.2025
```

## Cron-Job (jeden Sonntag 19:00)

```bash
0 19 * * 0 /usr/bin/python3 /opt/putzplan/putzplan_reminder.py
```

## Erwartetes Tabellenformat

Der Bot erwartet eine Markdown-Tabelle im folgenden Format:

```
| 02.06.2025 - 08.06.2025 | @user1 | @user2 |
| 09.06.2025 - 15.06.2025 | @user3 | @user4 |
```

## Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | Erfolg (oder Duplikat erkannt) |
| 1 | Konfigurationsfehler |
| 2 | API-/Netzwerk-Fehler |
| 3 | Daten-Fehler (keine Wochen gefunden etc.) |
