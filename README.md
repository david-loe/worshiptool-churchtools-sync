# WorshipTools → ChurchTools Sync

Mandantenfähige Sync-Plattform mit REST-Backend, dauerhaftem Scheduler/Worker,
Run-Historie, Benachrichtigungen und installierbarer Web-App. WorshipTools bleibt
führend für die konfigurierten Song-Slots einer ChurchTools-Agenda.

## Plattform starten

Voraussetzungen sind Docker mit Compose v2, ein öffentlicher HTTPS-Reverse-Proxy
und SMTP-Zugang für Registrierung, Recovery und Benachrichtigungen.

```bash
cp .env.example .env
install -d -m 700 secrets
# Nicht-sensitive Werte in .env anpassen und Secret-Dateien befüllen.
docker compose up --build
```

`.env` enthält nur Konfiguration und Pfade. Die referenzierten Dateien
unter `secrets/` werden als Compose-Secrets eingebunden und sind per `.gitignore`
ausgeschlossen. Erforderlich sind vier verschiedene PostgreSQL-Passwörter und
die dazu passenden Owner-, API-, Worker- und Admin-DSNs, außerdem Redis-URL,
Application-Secret, Encryption-Secret und eine JSON-Datei mit normalerweise
`{}` für alte Encryption-Keys. SMTP-, VAPID-, Telegram- und Bootstrap-Secrets
liegen ebenfalls in eigenen Dateien; ungenutzte optionale Dateien dürfen leer
sein. Dateien mit Passwörtern nie per Kommandozeilenargument oder Shell-History
befüllen, sondern aus dem Secret-Manager oder einem geschützten Editor schreiben.
Die vier Passwortdateien enthalten jeweils den unveränderten Rohwert. In den
vier DSN-Dateien muss derselbe Wert dagegen URL-percent-encoded sein, sobald er
reservierte URI-Zeichen enthält.
Alle Dateien gehören einer dedizierten Host-Gruppe, deren numerische ID als
`SECRETS_GID` konfiguriert ist, und erhalten Modus `0640`; nur diese Gruppe wird
den unprivilegierten Backend- und PostgreSQL-Prozessen zusätzlich zugewiesen.

Die PWA wird standardmäßig nur auf `127.0.0.1:8080` bereitgestellt. Der
vorgeschaltete Host-Reverse-Proxy terminiert TLS und leitet auf diesen Port
weiter. Eine abweichende Bind-Adresse muss bewusst gesetzt werden. Datenbank
und Redis besitzen keine öffentlichen Ports. Nur der Caddy-Gateway-Container
hängt zusätzlich am veröffentlichungsfähigen Edge-Netz; die API erreicht ihn
über ein separates internes Netz und besitzt keine Edge-Anbindung. Sync- und
Benachrichtigungsjobs
laufen in getrennten Queues, damit ein langsamer SMTP-Server keinen Sync
verdrängt. Sync-Worker lassen sich horizontal skalieren:

```bash
docker compose up --scale worker=4 -d
```

Der Datenbank-Owner-Schlüssel wird ausschließlich Migration und dem expliziten
Bootstrap-Werkzeug gegeben. API und Worker/Scheduler besitzen verschiedene
Rollen; der API-Adminpfad verwendet zusätzlich eine eng begrenzte Admin-Rolle.
Für den ersten Plattform-Admin werden E-Mail und Bootstrap-Secret kurz
konfiguriert und anschließend ausgeführt:

```bash
docker compose --profile tools run --rm bootstrap-admin
```

Danach die Bootstrap-Werte aus der Umgebung entfernen, neu deployen, anmelden
und unter „Konto“ sofort TOTP aktivieren. Admin-Funktionen werden erst mit einer
frisch per TOTP bestätigten Sitzung freigeschaltet.

Nach E-Mail-Verifikation führt die PWA durch Workspace, Provider-Verbindungen,
Profil, Preview und Aktivierung. Die interne API-Dokumentation liegt unter
`/api/v1/openapi.json`; die interaktive Swagger-Oberfläche `/api/v1/docs`
wird nur außerhalb der Produktionsumgebung aktiviert.

Weitere Dokumente:

- [Architektur](docs/architecture.md)
- [Sicherheitsmodell](docs/security.md)
- [Cutover vom Legacy-CLI](docs/cutover.md)

## Entwicklung und Tests

```bash
python3 -m pytest -q tests
python3 -m pip install --require-hashes --no-deps -r backend/requirements-test.lock
PYTHONPATH=backend python3 -m pytest -q backend/tests
pnpm --dir frontend install
pnpm --dir frontend test
pnpm --dir frontend build
```

Backend und Frontend besitzen eigene Dockerfiles. Direkte und transitive
Abhängigkeiten werden exakt gelockt (Python inklusive Distributions-Hashes) und
über CI gemeinsam mit Migration, Compose-Konfiguration und Images geprüft.
Auch das Legacy-Image installiert ausschließlich den gehashten
`requirements-runtime.lock`; Testwerkzeuge liegen getrennt im
`requirements-legacy.lock` und gelangen nicht ins Produktionsimage.
Alle direkten Backend-Pins und – mit Ausnahme von TypeScript 7, das derzeit den
Vue-Typechecker bricht – alle Frontend-Pins entsprechen dem Registry-Stand vom
22. August 2026. Updates erfolgen bewusst zusammen mit Tests und neuem Lockfile.

## Betrieb, Backup und Schlüsselrotation

PostgreSQL muss regelmäßig mit einem extern aufbewahrten `pg_dump` gesichert
und durch einen Restore-Test geprüft werden; Redis enthält nur Queue- und
Lease-Zustand und ersetzt kein Backup. Vor jedem Upgrade läuft zuerst
`alembic upgrade head` im Migrationscontainer, bevor API oder Worker starten.

### PostgreSQL-Zugangsdaten rotieren oder Rollen nachrüsten

Der PostgreSQL-Init-Hook läuft automatisch nur bei einem leeren Datenvolume.
Nach Änderung der vier Passwortdateien müssen auch die vier DSN-Dateien
denselben neuen Stand enthalten. Anschließend werden die Backend-Verbindungen
angehalten, PostgreSQL mit den aktuellen Secret-Mounts neu erstellt und der
idempotente Hook explizit erneut ausgeführt:

```bash
docker compose stop api scheduler worker notification-worker
docker compose up -d --no-deps --force-recreate postgres
docker compose exec -T postgres sh /docker-entrypoint-initdb.d/001-create-app-role.sh
docker compose run --rm migrate
docker compose up -d --force-recreate api scheduler worker notification-worker
```

Der Hook aktualisiert Owner, API, Worker und Admin per `ALTER ROLE`, liest alle
Passwörter ausschließlich aus den gemounteten Dateien und schreibt sie weder in
Argumentlisten noch Logs. Das anschließende Recreate verwirft offene
Connection-Pools mit alten Zugangsdaten. Derselbe Ablauf ist vor dem ersten
Upgrade auf Revision `0008` bei einem bestehenden Datenvolume erforderlich.
Auch bei jeder Rotation gilt: Passwortdateien enthalten den Rohwert; der
Passwortanteil der korrespondierenden DSN-Dateien ist URL-percent-encoded.

Für eine Rotation des AES-GCM-Masterschlüssels:

1. Datenbank sichern und API, Scheduler sowie beide Worker stoppen.
2. Den alten Schlüssel als JSON unter seiner Versionsnummer in die von
   `WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS_FILE` referenzierte Datei eintragen,
   `WT_SYNC_ENCRYPTION_KEY_VERSION` erhöhen und die von
   `WT_SYNC_ENCRYPTION_SECRET_FILE` referenzierte Datei ersetzen.
3. Einmalig den Rotationslauf ausführen:

   ```bash
   docker compose run --rm notification-worker python -m app.rotate_encryption
   ```

4. Dienste starten und Provider, TOTP sowie Push prüfen. Erst danach den alten
   Schlüssel aus dem Keyring entfernen und die Container erneut erstellen.

Der Rotationslauf ist transaktional. Er verschlüsselt Provider-Zugänge,
TOTP-Secrets, Push-Abonnements und noch aufbewahrte Outbox-Nachrichten neu.

## Legacy-CLI

Der bisherige Einmal-Sync bleibt nur als Quellcode- und Testreferenz im
Repository. Seine produktiven `config.yaml`-/`db.yaml`-Dateien und sein
Compose-Startpfad wurden entfernt, damit es genau einen schreibenden
Betriebsweg gibt. Bestehende Legacy-Dateien werden nicht importiert und sollten
nur offline als Cutover-Referenz archiviert werden.
