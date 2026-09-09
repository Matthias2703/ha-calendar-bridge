# Calendar Bridge — HA Custom Integration für native Kalender-Erinnerungen

## Kontext

HA-Kern kann keine Erinnerung/Alarm in einem Kalendereintrag setzen — auf keinem Backend. Der gemeinsame `CalendarEvent`-Datentyp (`homeassistant/components/calendar/__init__.py`) hat schlicht kein Reminder-Feld, egal ob Google, CalDAV oder lokaler Kalender. Zusätzlich lehnt `calendar.create_event` den Schlüssel `rrule` aktuell hart ab (`invalid_format: extra keys not allowed`), obwohl das Datenmodell ihn kennt — Wiederholungen sind über den öffentlichen Service also auch nicht sauber möglich.

Du hast eine automatisierungs-basierte Notification als Ersatz explizit ausgeschlossen ("nix automation" — die Erinnerung muss echt im Google/iOS-Kalender selbst stehen), n8n als Zwischenplattform explizit als überdimensioniert abgelehnt ("riesen Ding" für einen einfachen API-Call). Dein Vorschlag: eine eigene, HACS-taugliche Custom Integration bauen, die direkt gegen die Google-Calendar-API bzw. per CalDAV-PUT gegen iCloud spricht und dabei den Reminder/VALARM selbst mitgibt — sauber genug, um es potenziell echt zu veröffentlichen, da du das für ein Feature hältst, das auch andere brauchen könnten.

Ausgangslage, bereits verifiziert:
- **HACS ist bereits installiert** (`custom_components/hacs` läuft auf dem Host).
- **Google**: Core-Integration `google` (Konto `matthias.vierling@gmail.com`) existiert bereits, inkl. eigenem Google-Cloud-OAuth-Client (`application_credentials`, Domain `google`, Name „HomeAssist"). Dieser Client kann für die neue Integration wiederverwendet werden (siehe Risiken unten).
- **CalDAV**: Core-Integration `caldav` existiert bereits (iCloud, `https://caldav.icloud.com`), Zugangsdaten-Schema ist simpel: `{url, username, password, verify_ssl}`.
- Kein SFTP auf dem HA-Host — nur SSH-Exec-Kanal, das bisherige `cat | ssh "sudo sh -c 'cat > ...'"`-Muster funktioniert nur pro Einzeldatei.

## Architektur-Entscheidung: eigenständige, service-only Integration statt Kalender-Duplikat

Die neue Integration **liest nichts** und legt **keine neuen Entities** an — die bestehenden `google`/`caldav`-Kalender-Entities bleiben unangetastet. Sie stellt ausschließlich einen Service `calendar_bridge.create_event` bereit, der **direkt** die Google-Calendar-REST-API bzw. per CalDAV-PUT ein selbstgebautes ICS mit `VALARM`-Block anspricht — und umgeht damit `calendar.create_event`s Schema-Einschränkung komplett. Als Nebeneffekt kann der Service dadurch auch ein echtes `RRULE` mitgeben, was der Core-Service verweigert.

**Domain-Name-Vorschlag: `calendar_bridge`** ("Calendar Bridge"). Kurz, beschreibt die Rolle (Brücke zwischen HA-Automation und echtem Kalender-Backend), keine Namenskollision mit bestehenden HACS-Repos gefunden.

**Eigenes GitHub-Repo UND eigenes lokales Verzeichnis, komplett getrennt von diesem Config-Mirror-Repo.** Eine echte HACS-Integration lebt in ihrem eigenen Repo mit eigenen Releases/Tags (das ist es, was HACS beim Installieren zieht). Lokal entsteht dafür ein eigenständiger Ordner als Geschwisterverzeichnis, z. B. `C:\Users\blackbird\ha-calendar-bridge`, mit eigenem `git init`/`git clone` — **nicht** als Unterordner von `C:\Users\blackbird\homeassistant`. Ein verschachteltes Git-Repo (`.git` innerhalb eines anderen Git-Repos) ist ein bekannter Stolperstein (Git-Tools können den inneren Ordner falsch behandeln, Secrets/Historie könnten sich versehentlich vermischen); ein sauber getrenntes Verzeichnis vermeidet das komplett und passt zum eigentlichen Verteilweg: lokal entwickeln/testen (z. B. gegen eine Docker-Wegwerf-HA-Instanz), produktiv dann ganz normal per HACS installieren — dafür muss der Quellcode auf dem Windows-Rechner gar nicht in der Config-Repo-Struktur liegen.

**Datenmodell pro Ziel-Kalender, nicht pro Konto**: ein Google-Account-Konfigeintrag kann mehrere "Ziele" haben (z. B. „Matthias privat" und „Eva & Matthias Geburtstage"), ebenso ein CalDAV-Konto mehrere iCloud-Kalender. Umgesetzt über **Config Subentries** (der seit 2025 offizielle HA-Mechanismus genau für „ein Auth-Eintrag + mehrere Unter-Ziele", moderner als eine Options-Flow-Bastellösung). Jedes Subentry bekommt ein eigenes **Device**, damit der Service-Aufruf im UI einen sauberen Geräte-Picker statt roher Config-Entry-IDs zeigt.

## Datei-Layout

**`custom_components/calendar_bridge/`** (das eigentliche Integrationspaket):
```
__init__.py                 # async_setup (registriert den EINEN globalen create_event-Service), async_setup_entry/unload/reload
manifest.json                # domain, integration_type: service, iot_class: cloud_polling, requirements: icalendar/caldav
const.py
config_flow.py                # ConfigFlow (Menü: google/caldav) + ConfigSubentryFlow (weiteren Kalender hinzufügen)
application_credentials.py    # async_get_authorization_server -> Google-Endpunkte
api.py                        # dünner async Google-Calendar-REST-Client (aiohttp + OAuth2Session)
target.py                     # EventSpec/ReminderSpec-Dataclasses + CalendarTarget-Interface
google_target.py              # GoogleCalendarTarget
caldav_target.py              # CalDavCalendarTarget (baut ICS via `icalendar`, PUT via `caldav`)
services.py                   # create_event-Handler, Device->Target-Auflösung
device.py
diagnostics.py                # redaktiert Tokens/Passwörter
services.yaml
strings.json / translations/{en,de}.json
icons.json
brand/{icon.png,logo.png}     # seit HA 2026.3 lokal möglich, kein externer brands-PR nötig
quality_scale.yaml
```

**Repo-Root** (Standard-HACS-Layout, z. B. vom `ludeeus/integration_blueprint`-Template):
```
custom_components/calendar_bridge/   (s.o.)
tests/{conftest,test_config_flow_google,test_config_flow_caldav,test_config_flow_reauth,
       test_config_subentry_flow,test_init,test_google_target,test_caldav_target,test_services}.py
.github/workflows/{validate,lint,test}.yml
pyproject.toml, requirements_test.txt, hacs.json, README.md, LICENSE (MIT), CODEOWNERS
```

## Config Flow & Datenmodell

**Google, neues Konto**: Menü-Schritt (google/caldav) → Google verlangt vorher einen unter der **neuen** Domain registrierten Application-Credential-Eintrag (Settings → Application Credentials → Add, gleiche Client-ID/Secret wie beim bestehenden `google`-Eintrag wiederverwenden — siehe Risiko unten) → Standard-OAuth2-Redirect (Scope `https://www.googleapis.com/auth/calendar.events`, das ist "nur Termine lesen/schreiben", nicht volle Kalenderverwaltung) → nach Consent: `calendarList.list` abfragen, auf `accessRole in {owner,writer}` filtern → Kalender-Auswahl-Formular → Entry + erstes Subentry werden zusammen angelegt.

**Weiteren Google-Kalender später hinzufügen**: über die Device-Karte im UI ("Kalender hinzufügen") → `ConfigSubentryFlow` nutzt das bereits gültige OAuth-Token des Parent-Entry wieder, fragt `calendarList.list` erneut ab, kein erneuter Consent-Screen.

**CalDAV, neues Konto**: Formular (`url`, `username`, `password`, `verify_ssl`, Default wie beim Core-Eintrag) → **test-before-configure**: `caldav.DAVClient(...).principal().calendars()` aufrufen, 401 → `invalid_auth`, Verbindungsfehler → `cannot_connect` → Kalender-Auswahl → Entry + Subentry anlegen.

**Reauth**: Google via `ConfigEntryAuthFailed` bei abgelaufenem Token → Standard-Reauth-Schritt, alle Subentries/Devices bleiben unberührt. CalDAV via 401 bei einem Service-Aufruf → Formular nur mit neuem Passwort-Feld.

| Ort | Inhalt |
|---|---|
| `ConfigEntry.data` (Google) | OAuth-Token, verwaltet über `config_entry_oauth2_flow` |
| `ConfigEntry.data` (CalDAV) | `url`, `username`, `password`, `verify_ssl` |
| `ConfigSubentry.data` | Google: `{calendar_id}` · CalDAV: `{calendar_url, display_name}` |
| `ConfigEntry.runtime_data` | ein `CalendarTarget`-Client pro Account (geteilt über alle Subentries) |
| Device Registry | ein Device pro Subentry — macht den `target: {device: ...}`-Selector im Service-UI möglich |

## Service-Design

`services.yaml` (Auswahl der wichtigsten Felder):
```yaml
create_event:
  target:
    device: { integration: calendar_bridge }
  fields:
    summary:    { required: true, selector: { text: } }
    start:      { required: true, selector: { datetime: } }
    end:        { selector: { datetime: } }
    all_day:    { selector: { boolean: } }
    description:{ selector: { text: { multiline: true } } }
    location:   { selector: { text: } }
    reminder_minutes: { selector: { number: { min: 0, max: 40320, mode: box } } }  # einfacher Fall
    reminders:  { selector: { object: } }   # mehrere/E-Mail-Erinnerungen, max. 5
    rrule:      { selector: { text: } }     # Bonus: echte Wiederholung, da wir Cores Schema umgehen
```
`reminder_minutes` deckt den Standardfall (ein Popup-Alarm) ab; das mächtigere `reminders` (Liste `{method, minutes_before}`) ist für mehrere Offsets/E-Mail-Alarm gedacht. Der Service wird einmalig in `async_setup` registriert (nicht in `async_setup_entry`, das ggf. mehrfach läuft).

Gemeinsames Interface (`target.py`):
```python
class CalendarTarget(Protocol):
    async def async_create_event(self, calendar_ref: str, spec: EventSpec) -> str: ...
```

**CalDAV**: ICS inkl. `VALARM` wird mit der `icalendar`-Bibliothek von Hand gebaut (volle RFC-5545-Kontrolle, mehrere Alarme, `EMAIL`/`DISPLAY`-Action, RRULE) und über `caldav`s Low-Level-`Calendar.save_event(ical_text)` gespeichert — nicht über `caldav`s eigenen Komfort-Helper `add_event()`, der nur einen einzelnen simplen Alarm kann.

**Google**: `POST /calendar/v3/calendars/{id}/events` mit
```python
"reminders": {"useDefault": False, "overrides": [{"method": "popup"|"email", "minutes": N}, ...]}  # max. 5
"recurrence": [f"RRULE:{spec.rrule}"]  # falls gesetzt
```
über `aiohttp_client.async_get_clientsession(hass)` (nie eine eigene `ClientSession`) authentifiziert per `OAuth2Session.async_get_access_token()`.

## Deployment: zwei getrennte Loops

**Entwickeln**: lokal gegen `pytest` + `pytest-homeassistant-custom-component` (mockt Google-API/CalDAV an der Client-Grenze, Sekunden pro Lauf) sowie optional eine Wegwerf-HA-Instanz in Docker für echte End-to-End-Smoketests gegen deine echten Google-/iCloud-Konten, ohne den Produktiv-Pi anzufassen.

**Produktiv-Auslieferung = ganz normale HACS-Installation**: Sobald das Repo existiert und ein erstes Tag/Release hat, wird es auf dem echten HA einfach als HACS-Custom-Repository hinzugefügt und über die HACS-UI installiert/aktualisiert — kein manuelles SSH/tar nötig. Der SSH-Exec-Tar-Workaround (Ordner lokal packen, per `tar -czf - | ssh homeassistant "sudo sh -c 'tar -xzf - -C ...'"` entpacken) ist nur für **Vorab-Tests auf dem echten Host vor dem ersten Tag** gedacht, falls du zwischendurch gegen die echten Konten testen willst, bevor die Integration überhaupt HACS-fähig ist.

**Wichtig**: Python-Quelltext-Änderungen an einer Custom Integration werden von HA beim Modul-Import gecacht — ein bloßes "Neu laden" reicht bei Code-Änderungen nicht, es braucht einen vollständigen `ha core restart` (reine Datenänderungen wie ein neu hinzugefügtes Subentry oder ein Reauth brauchen dagegen keinen Neustart).

## Qualitäts-/Test-/CI-Plan (HACS-/Quality-Scale-tauglich)

Ziel: **Bronze + Silver** der HA Integration Quality Scale vollständig erfüllen (Gold/Platinum als spätere Kür, im `quality_scale.yaml` ehrlich als `todo`/`exempt` markiert statt weggelassen).

- **Bronze** u. a.: `config-flow`, `config-flow-test-coverage`, `test-before-configure`, `test-before-setup`, `unique-config-entry`, `runtime-data`, `action-setup`, `brands` (seit HA 2026.3 lokal im `brand/`-Ordner lösbar, kein externer PR mehr nötig), `docs-*`.
- **Silver** u. a.: `action-exceptions` (übersetzte `HomeAssistantError`/`ServiceValidationError` statt roher Exceptions), `reauthentication-flow`, `config-entry-unloading`, **`test-coverage` ≥ 95 %**.
- **CI** (`.github/workflows/`): `hassfest`-Validierung + offizielle `hacs/action` (Category `integration`), `ruff check`/`ruff format --check`/`mypy --strict`, `pytest --cov=... --cov-fail-under=95` (Google-API über `aioresponses`/`aioclient_mock` gemockt, CalDAV über gemocktes `caldav.DAVClient` — nie echte Accounts in CI).

## Phasenplan

1. **CalDAV-only `create_event` mit VALARM** — schnellstes Ende-zu-Ende-Feedback, kein OAuth nötig. Baut dabei das komplette gemeinsame Grundgerüst (EventSpec/CalendarTarget-Interface, Device-pro-Subentry, Service-Dispatch), das Phase 2 kostenlos mitnutzt. *Aufwand: mittel.*
2. **Google OAuth + Google `create_event`** — Application-Credentials-Wiederverwendung empirisch prüfen (siehe Risiko), OAuth-Zweig des Config-Flows, `reminders.overrides`/`recurrence`, Reauth. *Aufwand: mittel-hoch — die OAuth-Verdrahtung ist der komplexeste Teil des ganzen Projekts.*
3. **Politur**: Subentry-Flow „Kalender hinzufügen" für beide Backends, Device-Verwaltung, vollständige `de.json`/`en.json`, Diagnostics, Fehlermeldungen (`cannot_connect`, `invalid_auth`, `calendar_not_found`, Reminder-Limit …). *Aufwand: mittel.*
4. **CI/Tests/HACS-Verpackung** — Testsuite auf ≥95 %, `quality_scale.yaml`, GitHub Actions, README mit Badges, erstes Tag, als HACS-Custom-Repository hinzufügen. *Aufwand: mittel, größtenteils mechanisch.*

## Offene Risiken (vor Umsetzung/während Phase 2 zu klären)

- **Wiederverwendung der bestehenden Google-Client-ID/Secret unter der neuen Domain** ist nicht explizit dokumentiert, aber mit hoher Wahrscheinlichkeit unproblematisch (Application Credentials sind pro `(domain, client_id)` gespeichert, Google validiert nur die Redirect-URI, nicht die HA-Domain). **Fallback**, falls es doch nicht geht**: im selben, bereits existierenden Google-Cloud-Projekt einfach einen zweiten OAuth-Client anlegen (2-Minuten-Aufgabe, kein neues Projekt nötig).
- **„Eva & Matthias Geburtstage"-Kalender**: muss ein echter, beschreibbarer Zweitkalender sein (nicht Googles automatisch generierter, schreibgeschützter „Geburtstage"-Systemkalender) — in Phase 2 per `accessRole`-Check verifizieren.
- Exakte aktuelle Versionsnummern von `icalendar`/`caldav` (PyPI) zum Zeitpunkt der Umsetzung pinnen, `hassfest` verlangt exakte `==`-Pins.

## Verifikation

1. Phase 1: `pytest` grün, danach realer CalDAV-Testlauf gegen einen echten iCloud-Kalender — Termin erscheint mit Erinnerung in der iOS-Kalender-App.
2. Phase 2: gleicher Test gegen Google — Termin + Erinnerung erscheint in Google Kalender / iOS-Kalender-App (falls dort synchronisiert).
3. Vor jedem Produktiv-Deploy: `pytest --cov` ≥ 95 %, `ruff`/`mypy` sauber, `hassfest`- und `hacs/action`-Workflows grün.
4. Laufende Service-Aufrufe testen: einfacher Reminder, mehrere Reminder, E-Mail-Reminder, `rrule`-Wiederholung, All-Day-Termin, Fehlerfälle (falscher Device, Reminder-Minuten außerhalb 0–40320, mehr als 5 Overrides).

## Update 2026-09-09: Entscheidungen & Erweiterung

Repo ist angelegt und lokal initialisiert: [github.com/Matthias2703/ha-calendar-bridge](https://github.com/Matthias2703/ha-calendar-bridge) (leer, `main`-Branch, `origin` gesetzt).

**v1-Scope geändert**: Erstes Release enthält **CalDAV UND Google zusammen**, nicht mehr CalDAV-only zuerst. Phase 1 und 2 aus dem Phasenplan oben werden also nicht mehr nacheinander released, sondern beide vor dem ersten Tag fertiggestellt — die interne Reihenfolge (erst CalDAV-Grundgerüst, dann Google-OAuth obendrauf) bleibt als Entwicklungsreihenfolge sinnvoll, nur der Release-Schnitt verschiebt sich nach hinten auf „beide Backends fertig".

**Neues Feature: HA-native Erinnerung als Alternative zu Google/iOS-Reminder.** Zusätzlich zum nativen `VALARM`/`reminders.overrides` (das nur *im* Google-/iOS-Kalender selbst pingt) soll `create_event` optional eine **Home-Assistant-Benachrichtigung** zu einem definierten Zeitpunkt vor dem Termin auslösen — unabhängig vom Kalender-Backend. Das ist *kein* HACS-Update-Hinweis, sondern eine echte neue Zustellart neben Popup/E-Mail.

Das erfordert eine neue Komponente, da es (anders als der Rest der Integration) einen **Zeitplan/State** braucht, der einen HA-Neustart überlebt:
- `reminder_scheduler.py`: nimmt `{notify_target, minutes_before, message?}` entgegen, berechnet `fire_at = event.start - minutes_before`, plant den Callback über `async_track_point_in_time`.
- Persistenz über `homeassistant.helpers.storage.Store` (eigene `.storage/calendar_bridge_reminders`-Datei), damit geplante Erinnerungen einen Neustart überleben. Beim Setup werden gespeicherte Erinnerungen neu geplant; bereits verstrichene (z. B. HA war während des Fälligkeitszeitpunkts offline) werden sofort nachgeholt, außer sie liegen zu weit in der Vergangenheit (Schwelle TBD, Vorschlag: >1h alt → verwerfen statt spät nachzuholen).
- Auslösung per `notify.send_message` (entity-basierter Notify-Service, seit HA 2024.9) gegen ein vom Nutzer gewähltes Notify-Target (z. B. `notify.mobile_app_<gerät>`).
- Neues Service-Feld (Name TBD bei Implementierung, Arbeitsname `ha_notify`): `{ target: <notify-entity>, minutes_before: int, message: optional }`, orthogonal zu `reminder_minutes`/`reminders`/`rrule` nutzbar (kann zusätzlich zu oder anstelle von nativen Remindern gesetzt werden).
- Das ist NICHT Teil des gemeinsamen `CalendarTarget`-Interfaces (`google_target.py`/`caldav_target.py`), sondern läuft backend-unabhängig in `services.py` nach erfolgreichem `async_create_event`-Call.

**Default-Einstellungen (Options-Flow pro Subentry/Device)**, ab jetzt Teil des Datenmodells:
- **Standard-Reminder-Minuten** — greift, wenn ein `create_event`-Call weder `reminder_minutes` noch `reminders` setzt.
- **Standard-Ziel-Kalender** — ein Subentry/Device kann als Default markiert werden; greift, wenn der Service ohne `target: {device: ...}` aufgerufen wird. (Bewusst *nicht* gewählt: Standard-Event-Dauer — bleibt bei explizitem `end`-Feld ohne Default, `all_day`/Ende muss der Aufrufer weiter angeben.)
- **Standard-Reminder-Methode** — z. B. „popup" vs. „email" als Default, wenn `reminders`-Einträge keine Methode angeben.

Diese drei Defaults landen in `ConfigSubentry.data` (bzw. `ConfigSubentry.options`, sauberer über einen `ConfigSubentryFlow`-Options-Schritt) — s. Tabelle „Ort/Inhalt" oben, die um diese Felder ergänzt wird sobald Phase 1 beginnt.

**Kleine Korrektur am Manifest**: `iot_class` wird `cloud_push` statt `cloud_polling`, da die Integration nie pollt (keine Entities, reiner Service-Dispatch gegen Cloud-APIs).

**Repo-Grundgerüst (dieser Commit)**: Git-Struktur, README, LICENSE (MIT), `hacs.json`, `manifest.json`-Skelett, `.github/workflows` (hassfest + hacs/action, ruff/mypy, pytest), `pyproject.toml`. Bewusst noch **ohne** `services.py`/`config_flow.py`/Target-Implementierungen — die kommen mit der eigentlichen Phase-1-Umsetzung, um keine Datei-Leichen mit falschem Stand einzuchecken.

## Update 2026-09-09 (Fortsetzung): Phase-1-Implementierung + Live-Test auf echtem HA

Lokale Python-Umgebung ist auf Python 3.13/3.14 limitiert (dieser Rechner hat nur 3.12, aktuelles HA-Core verlangt inzwischen 3.14.2) — echtes Verifizieren von HA-internen APIs (Config Subentries, Device Registry) läuft daher **direkt auf dem echten HA-Host** (SSH-Alias `homeassistant`, `192.168.2.200`) statt lokal: Deploy per `tar | ssh … tar -xzf`, danach manueller Neustart durch dich (Settings → System → Restart), da die SSH-Verbindung über das "Advanced SSH & Web Terminal"-Addon im Protection Mode läuft und daher weder `ha core restart` noch Docker-Zugriff erlaubt — das lassen wir bewusst so (Protection Mode ist eine bewusste Sicherheitsentscheidung von dir).

Umgesetzt und **live auf dem echten HA (2026.9.0) verifiziert**: `target.py`, `caldav_target.py` (inkl. echter Unit-Tests gegen ein gemocktes `caldav.DAVClient`), `device.py` (Device-pro-Subentry via `config_subentry_id`), `config_flow.py` (Menü → CalDAV neu/wiederverwenden → Kalenderauswahl → Subentry-Flow zum Nachträglich-Hinzufügen/Reconfigure), `services.py`, `reminder_scheduler.py`, `__init__.py`. Der komplette Config-Subentry/Device/Service-Mechanismus läuft nachweislich auf echtem, aktuellem HA durch.

**Neu gegenüber der ursprünglichen Spezifikation, aus echtem Nutzer-Feedback beim Live-Test:**
- **Zugangsdaten-Wiederverwendung**: Der Config Flow bietet jetzt zusätzlich "Bestehendes CalDAV-Konto wiederverwenden" an — liest `url/username/password/verify_ssl` direkt aus einem bereits vorhandenen Core-`caldav`-Eintrag (`hass.config_entries.async_entries("caldav")`), rein serverseitig im HA-Prozess, das Passwort läuft nie durch den Chat oder wird erneut eingetippt. Feldnamen sind identisch zum Core-`caldav`-Schema (`CONF_URL/CONF_USERNAME/CONF_PASSWORD/CONF_VERIFY_SSL`), am echten Host per `.storage/core.config_entries` verifiziert.
- **Mehrfachauswahl beim Ersteinrichten**: Die Kalenderauswahl beim ersten Setup eines Accounts erlaubt jetzt Mehrfachauswahl (ein Subentry pro gewähltem Kalender, alle mit denselben Default-Einstellungen aus demselben Formular). Das nachträgliche "Kalender hinzufügen" (Subentry-Flow an einem bestehenden Entry) bleibt bewusst Einzelauswahl, da ein Subentry-Flow-Durchlauf technisch nur genau einen Subentry erzeugen kann.
- **"Keine" als Standard-Erinnerungsmethode**: `default_reminder_method` hat jetzt einen dritten Wert `none` — damit erzwingt die Integration keinen Reminder, wenn ein `create_event`-Aufruf keinen eigenen angibt. Wichtig für Nutzer, die ausschließlich die native iOS/Google-Erinnerung explizit pro Termin wollen und nicht implizit irgendeinen Default-Alarm gesetzt bekommen möchten.
- **E-Mail-Erinnerung bei CalDAV ist NICHT verifiziert und daher kein Default-Angebot mehr**: Anders als Google Calendar (wo `reminders.overrides` mit `method: email` eine dokumentierte, zuverlässige Server-Funktion ist), gibt es keine Bestätigung, dass iCloud/CalDAV ein per Drittanbieter-PUT gesetztes `VALARM;ACTION:EMAIL` tatsächlich serverseitig verarbeitet und eine Mail verschickt. Das Standard-Erinnerungsmethode-Dropdown bietet bei CalDAV deshalb nur noch `popup`/`none` an (Google bekommt in Phase 2 wieder alle drei). Der generische `create_event`-Service-Parameter `reminders` erlaubt `method: email` weiterhin explizit (für den Fall, dass ein anderer CalDAV-Server als iCloud es doch unterstützt), aber ohne Erfolgsgarantie. **TODO Phase 1 Verifikation**: einmal real gegen iCloud testen, ob eine E-Mail ankommt — falls nicht, `email` als Methode für CalDAV in der Doku explizit als "nicht unterstützt" markieren statt nur "ungeprüft".
- **Frontend-Übersetzungs-Cache-Falle**: Während der Entwicklung fielen Config-Flow-Texte (Titel/Beschreibungen/Menü-Optionen) nach einem Deploy zunächst leer aus — reines Frontend-Caching-Problem (HA cacht Custom-Component-Übersetzungen aggressiv pro Browser-Session), behoben durch harten Reload (Strg+Shift+R). Kein Backend-Fehler. Für künftige Dev-Iterationen an `strings.json`/`translations/*.json` merken: nach Deploy immer hart neu laden, bevor man einen Übersetzungs-Bug vermutet.

## Update 2026-09-09 (Fortsetzung 2): `PutError at '404 Not Found'` gelöst — zwei echte Bugs, keine Auth/Host-Ursache

Der reale `create_event`-Aufruf schlug am echten iCloud-Account durchgehend mit `caldav.lib.error.PutError: PutError at '404 Not Found'` fehl, obwohl derselbe Client/dieselbe Kalender-Ressource für einen manuellen `caldav.Calendar.add_event(**kwargs)`-Diagnoseaufruf (Bibliotheks-eigene ICS-Erzeugung) einwandfrei funktionierte. Root-Cause-Suche per Quellcode-Vergleich (nicht Rätselraten): `caldav`s `Calendar.add_event` ist ein reiner Alias für `save_event` — beide laufen über denselben `save_object()` → `Event(...).save()` → `_create()` → `_put()`-Pfad. Der einzige Unterschied war also der tatsächliche ICS-Text, nicht der Transport-Code.

Zwei reale, sich überlagernde Bugs in `caldav_target.py`, beide durch Quellcode-Lektüre (HA-Core `config_validation.py`/`dt.py`, `python-caldav` v2.1.0) bestätigt, nicht durch Vermutung:

1. **Naive Datetimes → "floating time"**: HA's `cv.datetime`/`dt_util.parse_datetime` liefert ein **naives** `datetime`-Objekt, wenn der Service-Aufruf-String keinen UTC-Offset enthält (z. B. `"2026-09-09 17:30:00"` — genau das Format, das jeder Testaufruf in diesem Debugging benutzt hat). `icalendar` serialisiert ein naives datetime als "floating" lokale Zeit (kein `Z`, kein `TZID`) — laut RFC 5545 gültig, aber iCloud's CalDAV-Edge lehnt das mit einem generischen, bodyless 404 ab. **Fix**: `_as_utc()`-Helper in `caldav_target.py`, der jedes `datetime` (nicht `date`, für künftige Ganztags-Events) durch `homeassistant.util.dt.as_utc()` schickt, bevor es in `DTSTART`/`DTEND` landet — genau der idiomatische HA-Weg, naive Nutzereingaben als "in der konfigurierten Zeitzone gemeint" zu behandeln.
2. **Fehlendes `DTEND`**: RFC 5545 erlaubt ein `VEVENT` ganz ohne `DTEND`/`DURATION` (Null-Dauer), aber iCloud verlangt es in der Praxis trotzdem — jeder Testaufruf in diesem Debugging hatte zufällig kein `end` gesetzt. **Fix**: `_build_ical()` setzt jetzt immer ein `DTEND`, defaultet auf `DTSTART + 1 Stunde` (`_DEFAULT_EVENT_DURATION`), wenn `spec.end` fehlt.

Beide Fixes waren einzeln nötig — nach Fix 1 allein schlug ein Test ganz ohne `reminder_minutes` (zur Isolation von VALARM als Verdächtigem) immer noch fehl, weil der Kalender-Subentry einen konfigurierten Default-Reminder hatte (60 Min.) und **kein** `end` mitgab. Erst mit beiden Fixes zusammen liefen alle drei Szenarien live gegen den echten iCloud-Account durch: einfacher Termin, Termin mit echtem Popup-Reminder (VALARM), Termin mit `RRULE`-Wiederholung. Frühere Theorien (UID mit `@`-Zeichen im PUT-Dateinamen, VALARM-Struktur, Host-Redirect bei der Kalender-Discovery) waren zwar teils reale, separate Fixes (UID) bzw. bereits verifiziert funktionierender Code (Discovery), aber nicht die Ursache dieses konkreten 404.

Neuer Unit-Test `test_naive_start_is_normalized_to_utc_not_left_floating` und `test_missing_end_defaults_to_a_one_hour_dtend` in `tests/test_caldav_target.py` decken beide Fixes ab (9/9 Tests grün).

**Bekannte Lücke, nicht Teil dieses Fixes**: Die Integration kann bisher nur Termine anlegen — kein `delete_event`/`update_event` über die HA-Kalender-UI. Die während dieses Debuggings live angelegten Test-Termine (u. a. „DIAG add_event test", „diag with dtend", „Final verify with reminder", „Final verify rrule") müssen daher manuell (z. B. im iPhone-Kalender oder iCloud-Web) gelöscht werden, bis eine spätere Phase Delete/Update ergänzt.

## Update 2026-09-09 (Fortsetzung 3): Phase 1 abgeschlossen — All-Day-Events, verbleibende Live-Verifikation, CI grün

Rest des Phase-1-Verifikations-Checklisten abgearbeitet:

- **All-Day-Events implementiert**: `EventSpec.all_day` existierte im Datenmodell, wurde von `caldav_target.py` aber komplett ignoriert (`DTSTART`/`DTEND` liefen immer über den datetime-Pfad). Neuer `_all_day_bounds()`-Helper liefert `date`-Werte (kein `Z`, `VALUE=DATE` statt `VALUE=DATE-TIME`) und beachtet RFC 5545s exklusives `DTEND` (Ein-Tages-Termin braucht `DTEND = DTSTART + 1 Tag`, sonst zeigt der Termin 0 Tage Dauer). Live gegen iCloud verifiziert: erscheint korrekt als ganztägiger Banner, nicht als 1-Stunden-Termin.
- **Zweiter naiver-Datetime-Bug gefunden und gefixt**: Dieselbe Klasse von Bug wie beim CalDAV-404 steckte auch im HA-Notification-Reminder-Pfad (`services.py::_async_schedule_notification`) — `spec.start` ohne UTC-Normalisierung an `async_track_point_in_time` durchgereicht. Mit `dt_util.as_utc()` gefixt und live verifiziert: Push-Benachrichtigung an `notify.iphone` wurde fehlerfrei verschickt und die Reminder-Queue (`.storage/calendar_bridge_reminders`) korrekt nach Zustellung geleert.
- **E-Mail-Reminder bei CalDAV: jetzt negativ verifiziert statt nur "ungeprüft"**: Zwei Test-Termine mit `VALARM;ACTION:EMAIL` gegen den echten iCloud-Account (Apple-ID = `matthias.vierling@gmail.com`) angelegt und die Gmail-Inbox direkt danach geprüft — **keine E-Mail angekommen**, weder für den sofort fälligen noch den ~1,5 Std. späteren Alarm. Bestätigt die bisherige Vermutung: iCloud verarbeitet von Drittanbietern per CalDAV gesetzte `EMAIL`-VALARMs nicht. Sollte in der Doku/HACS-Beschreibung explizit als "von iCloud nicht unterstützt" markiert werden, nicht mehr als "ungeprüft".
- **Fehlerpfade verifiziert**: `device_not_found` live getestet (falsche `device_id` → sauberer übersetzter Fehler „Gerät ... ist kein Calendar-Bridge-Kalender."). `no_target`/`calendar_not_found` bleiben code-seitig plausibel, aber nicht live erzwungen (würde bedeuten, die konfigurierte Default-Markierung zu entfernen bzw. einen echten Kalender serverseitig zu löschen — beides unnötig riskant für reine Verifikation).

**CI war seit dem allerersten Phase-1-Commit rot** (nicht erst durch die 404-Fix-Änderungen verursacht) — beim Schließen von Phase 1 mitbehoben, da eine "HACS-taugliche" Integration ohne grüne CI nicht wirklich fertig ist:

- `requirements_test.txt` installierte nie `icalendar`/`caldav` (nur `pytest-homeassistant-custom-component`) → Test-Job scheiterte schon beim Import. Ergänzt, exakt auf die in `manifest.json` gepinnten Versionen (`icalendar==6.3.1`, `caldav==2.1.0`), plus `types-icalendar` für mypy.
- `mypy --strict` hatte 9 echte Typfehler (u. a. `Store`-Generic ohne Typargument, `MappingProxyType` vs. `dict` bei `subentry.data`, fehlende `ConfigSubentryData`-Typisierung, `dict`-Generic ohne Typargument in `__init__.py`) — alle gefixt. `caldav` hat keine Typstubs; per `[[tool.mypy.overrides]]` in `pyproject.toml` (`ignore_missing_imports`) plus zwei gezielte `# type: ignore[no-untyped-call]` an den `client.principal()`-Aufrufen abgefangen, statt Striktheit projektweit aufzuweichen.
- `services.yaml`: `target: {device: {integration: ...}}` ist laut aktuellem Hassfest-Schema nicht mehr erlaubt ("Services do not support device filters on target, use a device selector instead"). Umgebaut zu einem normalen `device_id`-Feld mit `selector.device.filter.integration` + `multiple: true` — funktional identisch (Service-Code liest ohnehin `call.data[ATTR_DEVICE_ID]`), live gegen den echten Host verifiziert (mit und ohne explizite `device_id`).
- `strings.json`/`translations/*.json`: fehlender Pflichtschlüssel `config_subentries.calendar.initiate_flow` ergänzt; URL aus `config.step.caldav.description` entfernt (Hassfest verbietet rohe URLs in übersetzbaren Strings); `device_id`-Feldbeschreibung zu `services.create_event.fields` ergänzt.
- `__init__.py`: `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)` ergänzt (Hassfest-Warnung, da die Integration `async_setup` ohne eigenes Schema definierte).
- GitHub-Repo-Metadaten nachgezogen (HACS-Validierung verlangt beides): Beschreibung und Topics (`home-assistant`, `hacs`, `hacs-integration`, `calendar`, `caldav`, `icloud`, `reminders`) gesetzt, vorher beides leer.
- Zwei HACS-Validierungsfehler (`integration_manifest`/`hacsjson`: "expected a dictionary. Got None") bleiben ungeklärt — vermutlich hängt diese HACS-Action-Prüfung an einem existierenden Release-Tag, den es erst in Phase 4 gibt. Nach dem ersten Release-Tag erneut prüfen.

Lokale Verifikation war durch die Python-3.12-Limitierung (siehe oben) unvollständig: `mypy --strict` lief lokal gegen das alte, extrahierte `homeassistant==2025.1.4`-Wheel (ohne Config Subentries), was 10 Fehler produzierte, die ausschließlich auf diese veraltete Stub-Version zurückzuführen sind (nicht auf echte Bugs) — abgeglichen mit den tatsächlichen 9 Fehlern aus dem echten CI-Lauf, die alle gezielt gefixt wurden. Der nächste Push zeigt, ob CI jetzt wirklich grün ist.

Phase 1 ist damit inhaltlich abgeschlossen: CalDAV-Backend, Config Subentries, Devices, Service, alle Kern-Szenarien (einfacher Termin, Reminder, mehrere Reminder, RRULE, All-Day) live verifiziert, E-Mail-Reminder als "nicht unterstützt" dokumentiert statt offen gelassen, HA-native Notification-Reminder live verifiziert, ein Fehlerpfad live verifiziert.

**CI-Ergebnis nach dem Push**: Test, Lint (`ruff` + `mypy --strict`) und Hassfest sind grün. Nur der HACS-Validierungsjob bleibt rot (2 von 9 Checks: `hacsjson`, `integration_manifest`) — Root Cause gefunden, keine Code-/Config-Bug: HACS' Validator lädt `hacs.json`/`manifest.json` über die öffentliche `raw.githubusercontent.com`-URL (unabhängig vom `github_token` der Action), die für ein **privates** Repo grundsätzlich 404 liefert (verifiziert per `curl` gegen `raw.githubusercontent.com/Matthias2703/ha-calendar-bridge/main/hacs.json` — 404, obwohl die Datei im Repo existiert und valide ist). Das Repo ist aktuell `private` (`gh repo view --json isPrivate` → `true`). Das ist inhaltlich korrekt: ein privates Repo ist über HACS ohnehin nicht installierbar (weder für andere Nutzer noch für HACS' eigene Prüfung), das war schon immer als "erster Release-Tag" für Phase 4 vorgesehen. **Entscheidung liegt bei dir**: Repo jetzt schon public schalten (dann sollte HACS komplett grün werden) oder bewusst noch private lassen, bis Phase 2/Google-Backend fertig ist — das ändere ich nicht ohne Rückfrage, da Sichtbarkeits-Umschaltung die komplette bisherige Historie öffentlich macht.

## Update 2026-09-09 (Fortsetzung 4): Reaktives Reminder-Backfilling für HA-native Termin-Erstellung

**Ausgangsproblem**: Der native HA-Kalender-Button „+ Termin hinzufügen" (bzw. jede andere Instanz, die `calendar.create_event` statt `calendar_bridge.create_event` aufruft — Siri-Shortcuts, Automationen, ...) erzeugt Termine über HA-Cores **eigenen** `caldav`-Kalender-Entity-Service, der komplett an `calendar_bridge` vorbeigeht und daher nie einen `VALARM` bekommt — genau die Lücke, für die diese ganze Integration existiert. Du wolltest keine Doku-Notiz „nutz stattdessen den Service", sondern dass das native „+" transparent mitfunktioniert.

**Mechanismus statt Duplikat-Entity**: Statt eine eigene `Calendar`-Plattform-Entity zu bauen (die den Kalender doppelt anzeigen würde), reagiert `calendar_bridge` jetzt auf HAs internes `EVENT_CALL_SERVICE`-Bus-Event (`homeassistant/core.py`, wird für **jeden** Service-Aufruf synchron **vor** der eigentlichen Ausführung gefeuert). Neuer Listener in `__init__.py::async_setup`, gefiltert auf `domain == "calendar"` und `service == "create_event"`:

1. Extrahiert `summary`/`start_date_time`/`start_date` aus den rohen (noch unvalidierten) `service_data`-Feldern des Core-Schemas (`homeassistant/components/calendar/const.py`).
2. Wartet `_BACKFILL_DELAY = 3` Sekunden — das eigentliche Schreiben auf den CalDAV-Server passiert asynchron *nach* dem Event, es gibt keinen Hook für „danach fertig geschrieben".
3. Sucht per neuem `CalDavCalendarTarget.async_backfill_reminder()` (in `caldav_target.py`) über `caldav`s `Calendar.date_search()` in einem ±1h-Fenster um die Startzeit nach einem Event mit exakt passendem `summary`, das noch **keinen** `VALARM` hat (`component.walk("VALARM")` leer), und hängt dann per `component.add_component()` + `event.save()` den konfigurierten Standard-Reminder des jeweiligen Kalender-Subentries nachträglich an — ein echtes In-Place-Update derselben CalDAV-Ressource (bestätigt durch Lektüre von `CalendarObjectResource.save()`, das die bereits bekannte URL wiederverwendet statt eine neue Ressource anzulegen).
4. Iteriert dabei über alle konfigurierten Konten/Subentries und bricht beim ersten Treffer ab; Subentries mit `default_reminder_method: none` werden übersprungen (kein aufgezwungener Reminder für Kalender, die das nicht wollen).

**Bekannte, bewusst akzeptierte Grenze**: Das fängt nur Termine ab, die **durch Home Assistant selbst** laufen (natives „+", Automation, Siri→HA-Shortcut). Ein Termin, der direkt in der iOS-Kalender-App angelegt wird (ohne HA im Signalweg), berührt HA nie und kann so nicht abgefangen werden — dafür gibt es keine Lösung ohne Polling, was hier bewusst nicht gebaut wurde.

**Live-Verifikation** (nicht über den eigentlich vorgesehenen nativen „+"-Button, siehe Blocker unten, sondern äquivalent end-to-end): ein reminder-loser Termin wurde über `calendar_bridge.create_event` (mit `reminders: []`) angelegt und anschließend ein synthetisches `call_service`-Event mit `domain: calendar`, `service: create_event`, passendem `summary`/`start_date_time` über Entwicklerwerkzeuge → Ereignisse gefeuert — das ist exakt das Signal, auf das der Listener reagiert, unabhängig von seiner Quelle. Nach der 3-Sekunden-Verzögerung erschien im Core-Log (per Terminal-Add-on, `ha core logs -f` in eine Datei umgeleitet, siehe unten) die erwartete Zeile:
```
INFO [custom_components.calendar_bridge.caldav_target] Backfilled a popup reminder onto 'Reactive Backfill Verify 2'
```
Ein zweiter, per echtem `calendar.create_event`-Aufruf (nicht synthetisch) angelegter Test-Termin bestätigte, dass die Kette bis zur Sync-Ausführung erreichbar ist — der erste Versuch schlug allerdings am unten beschriebenen Blocker fehl, bevor der Reminder gesetzt werden konnte, und wurde durch den zweiten Test (synthetisches Event auf denselben nativen Codepfad) ersetzt.

**Unabhängiger Blocker, keine Ursache in `calendar_bridge`**: HA-Cores eigene `caldav`-Integration (die tatsächliche Ziel-Plattform hinter dem nativen „+"-Button) schlägt aktuell bei **jedem** `calendar.create_event`-Aufruf mit „Unknown error" / `TimeoutError` tief im `niquests`/`urllib3`-Stack fehl (`components/caldav/calendar.py:240`). Isoliert bestätigt als eigenständiges, von `calendar_bridge` unabhängiges Problem: `calendar_bridge.create_event` funktioniert währenddessen einwandfrei gegen denselben iCloud-Account, und weder ein Reload der `caldav`-Config-Entry noch ein vollständiger HA-Neustart hat den Timeout behoben. Root Cause nicht weiter verfolgt (liegt außerhalb dieses Repos) — die native „+"-Verifikation mit echtem Button-Klick steht deswegen noch aus, sobald der Core-`caldav`-Bug (oder die zugrundeliegende Netzwerk-/Account-Ursache) behoben ist, sollte sie nachgeholt werden.

**Debugging-Fußnote — Log-Zugriff**: Die Standard-„Protokolle"-Seite (`/config/logs`) hält **nur** WARNING/ERROR im Arbeitsspeicher, auch bei aktivierter Debug-Protokollierung für eine Komponente — INFO/DEBUG-Zeilen landen zwar im echten Log-Stream, aber nicht in dieser UI. Zugriff über das „Terminal"-Add-on (`ha core logs`) nötig; zusätzlich flutet die extrem gesprächige `zendure_ha`-Integration den Log-Ringpuffer so schnell, dass ältere Zeilen (auch unsere Backfill-Bestätigung) binnen Minuten verdrängt werden — für den zweiten Testlauf wurde deshalb `ha core logs -f > /tmp/corelog.txt &` **vor** dem Auslösen des Test-Events gestartet, um die Zeile sicher einzufangen.

**Zwei Test-Termine bleiben im „Privat"-Kalender zurück** (`Reactive Backfill Verify`, `Reactive Backfill Verify 2`) — `calendar.delete_event` existiert nicht als Service in dieser HA-Version, und der Core-`caldav`-Timeout blockiert aktuell ohnehin jeden Schreibzugriff über die Kalender-UI. Manuelle Löschung (iPhone-Kalender oder iCloud-Web) steht noch aus, wie schon bei den Termini aus der 404-Debugging-Session.

Der Code (`__init__.py`, `caldav_target.py`) ist lokal fertig, ruff-clean, mypy-strict zeigt nur die bekannten Stub-bedingten Fehler — **noch nicht committet**, da die native End-to-End-Verifikation wegen des Core-`caldav`-Blockers aussteht.
