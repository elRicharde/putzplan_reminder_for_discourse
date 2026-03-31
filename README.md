# Putzplan-Reminder für Discourse

Ein Python-Bot, der wöchentlich einen Putzplan-Reminder in ein Discourse-Forum postet. Er liest eine Markdown-Tabelle aus einem konfigurierten Topic, ermittelt die kommende und übernächste Woche und erstellt automatisch einen Erinnerungs-Post.

## Features

- **Automatische Tabellen-Erkennung** — findet den neuesten Post mit gültiger Putzplan-Tabelle (mind. 3 Wochen) im Topic
- **Markdown- und HTML-Parsing** — parst raw-Markdown-Tabellen, mit Fallback auf cooked-HTML wenn der raw-Endpoint nicht verfügbar ist (z.B. bei eingeschränkten API-Berechtigungen)
- **Gruppen-Abgleich** — prüft gegen Discourse-Gruppen, wer noch keinen Putzdienst eingetragen hat
- **Intelligente Erinnerung** — zeigt nicht-eingetragene Mitglieder nur wenn ein Dienst unterbesetzt ist (< 2 Putzer), mit Fallback auf Mitglieder mit nur einem Eintrag
- **Bemerkungen-Spalte** — zählt auch @Mentions in der Bemerkungen-Spalte als Einträge
- **Duplikat-Schutz** — verhindert doppelte Reminder für dieselbe Woche (sucht spezifisch nach "Kommende Woche" um Verwechslungen mit dem Vorwochen-Reminder zu vermeiden)
- **Dynamischer Putzplan-Link** — jeder Reminder enthält einen direkten Link zum Putzplan-Post
- **Retry-Logik** — automatische Wiederholung bei Netzwerkfehlern und Rate-Limits
- **User-Ausschluss** — bestimmte User können vom Putzplan ausgenommen werden

## Voraussetzungen

- Python 3.7+
- `requests`
- Ein Discourse-Forum mit API-Key

## Installation

```bash
git clone https://github.com/elRicharde/putzplan_reminder_for_discourse.git
cd putzplan_reminder_for_discourse
pip install requests
cp .env.example .env
# .env mit eigenen Werten befüllen
```

Auf Ubuntu 23.04+ (mit "externally managed environment"):

```bash
sudo apt install python3-requests
```

## Konfiguration

Kopiere `.env.example` nach `.env` und trage die Werte ein:

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `DISCOURSE_URL` | Ja | Forum-URL (z.B. `https://forum.example.com`) |
| `DISCOURSE_API_KEY` | Ja | API-Key des Bot-Users |
| `DISCOURSE_API_USERNAME` | Ja | Bot-Username (z.B. `Putzbot`) |
| `DISCOURSE_TOPIC_ID` | Ja | Topic-ID des Putzplan-Topics |
| `DISCOURSE_SCHEDULE_POST_ID` | Nein | Interne Post-ID mit der Tabelle (sonst Auto-Erkennung) |
| `DISCOURSE_MEMBER_GROUPS` | Nein | Kommagetrennte Gruppen-Namen für den Abgleich |
| `DISCOURSE_EXCLUDE_USERS` | Nein | Kommagetrennte Usernames, die nie putzen müssen |

**Hinweis:** `DISCOURSE_SCHEDULE_POST_ID` ist die interne Post-ID, nicht die Nummer in der URL (`/t/slug/3513/95`). Findbar via API: `GET /t/{topic_id}.json` → `post_stream.stream[]`

### Benötigte API-Scopes

| Scope | Grund |
|---|---|
| `topics:read` | Topic und Posts laden, Putzplan-Tabelle lesen |
| `topics:write` | Reminder-Post erstellen |
| `groups:read` | Gruppen-Mitglieder für den Abgleich laden (nur wenn `DISCOURSE_MEMBER_GROUPS` gesetzt) |

## Verwendung

```bash
# Reminder erstellen
python putzplan_reminder.py

# Nur anzeigen, nicht posten
python putzplan_reminder.py --dry-run

# Duplikat-Schutz umgehen
python putzplan_reminder.py --force

# Datum simulieren (z.B. um nächste Woche zu testen)
python putzplan_reminder.py --date 15.06.2025 --dry-run
```

## Cron-Job (jeden Sonntag 19:00)

```bash
crontab -e
```

```
0 19 * * 0 /usr/bin/python3 /path/to/putzplan_reminder.py >> /path/to/cron.log 2>&1
```

## Erwartetes Tabellenformat

Der Bot erwartet eine Markdown-Tabelle mit 4-5 Spalten:

```
| Woche                     | Putzer 1 | Putzer 2 | Bemerkungen      |
| 02.06.2025 - 08.06.2025  | @user1   | @user2   |                  |
| 09.06.2025 - 15.06.2025  | @user3   |          | @user4 tauscht   |
| 16.06.2025 - 22.06.2025  |          |          |                  |
```

- **Spalte 1:** Datumsbereich im Format `dd.mm.yyyy - dd.mm.yyyy`
- **Spalte 2+3:** Putzer (mit oder ohne `@`-Prefix)
- **Spalte 4:** Bemerkungen (optional) — `@Mentions` hier zählen ebenfalls als Eintrag

## Reminder-Logik

Der Bot generiert den Reminder-Text nach folgenden Regeln:

1. **Kommende Woche**: Zeigt an, wer Putzdienst hat (oder warnt wenn niemand eingetragen ist)
2. **Übernächste Woche**: Zeigt Vorschau auf die Woche danach
3. **Nicht-eingetragene Mitglieder** (nur wenn ein Dienst < 2 Putzer hat):
   - Zuerst: Mitglieder die noch gar nicht im Plan stehen
   - Fallback: Mitglieder die erst einmal eingetragen sind
4. **Putzplan-Link**: Direkter Link zum Post mit der Tabelle

## Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | Erfolg (oder Duplikat erkannt) |
| 1 | Konfigurationsfehler |
| 2 | API-/Netzwerk-Fehler |
| 3 | Daten-Fehler (keine Wochen gefunden etc.) |
