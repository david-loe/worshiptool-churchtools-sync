# WorshipTools → ChurchTools Sync

Mandantenfähige Sync-Plattform mit REST-Backend, dauerhaftem Scheduler/Worker,
Run-Historie, Benachrichtigungen und installierbarer Web-App. WorshipTools bleibt
führend für die konfigurierten Song-Slots einer ChurchTools-Agenda.

## Plattform starten

Voraussetzungen sind Docker mit Compose v2, ein öffentlicher HTTPS-Reverse-Proxy
und SMTP-Zugang für Registrierung, Recovery und Benachrichtigungen.

```bash
cp .env.example .env
# Konfiguration und alle Secret-Platzhalter in .env ersetzen.
chmod 600 .env
docker compose up --build
```

Für ein Deployment mit den bereits veröffentlichten Images wird das
Image-Override zusätzlich eingebunden. Es baut auf dem Zielsystem keine Images:

```bash
docker compose -f compose.yaml -f deploy.yaml pull
docker compose -f compose.yaml -f deploy.yaml up -d --wait
```

Standardmäßig werden die `main`-Images aus der GitHub Container Registry
verwendet. Eine konkrete Version oder eine andere Registry kann explizit
gewählt werden:

```bash
WT_SYNC_BACKEND_IMAGE=ghcr.io/david-loe/worshiptools-churchtools-sync-backend:1.2.3 \
WT_SYNC_FRONTEND_IMAGE=ghcr.io/david-loe/worshiptools-churchtools-sync-frontend:1.2.3 \
docker compose -f compose.yaml -f deploy.yaml up -d --wait
```

Die Standard-Compose-Konfiguration reicht Secrets direkt aus der git-ignorierten
`.env` an die jeweils berechtigten Container weiter. Erforderlich sind vier
verschiedene PostgreSQL-Passwörter und die dazu passenden Owner-, API-, Worker-
und Admin-DSNs, außerdem Redis-URL, Application-Secret und Encryption-Secret.
Das JSON-Objekt für alte Encryption-Keys ist normalerweise `{}`; ungenutzte
SMTP-, VAPID- und Telegram-Werte dürfen leer sein. Passwörter nie als
Kommandozeilenargument eingeben, `.env` auf Modus `0600` beschränken und Werte
möglichst aus einem geschützten Secret-Manager bereitstellen. Secret-Werte in
Container-Umgebungen sind für Nutzer mit Docker-Daemon-Zugriff per Inspect
einsehbar.

Alle Secret-Einstellungen unterstützen alternativ eine `_FILE`-Variable. Dazu
muss ein eigenes Compose-Override die Datei in den Container mounten und dort
zum Beispiel `WT_SYNC_APPLICATION_SECRET_FILE=/run/secrets/application_secret`
setzen. Direktwert und `_FILE`-Variante dürfen nie gleichzeitig gesetzt sein.
Die vollständige Zuordnung steht kommentiert in `.env.example` und im
[Sicherheitsmodell](docs/security.md). Für API und Worker heißt die
containerinterne Alternative jeweils `WT_SYNC_DATABASE_URL_FILE`; das Override
mountet dort entsprechend die API- oder Worker-DSN. PostgreSQL unterstützt
`POSTGRES_PASSWORD_FILE`, `POSTGRES_API_PASSWORD_FILE`,
`POSTGRES_WORKER_PASSWORD_FILE` und `POSTGRES_ADMIN_PASSWORD_FILE`.

Die vier direkten PostgreSQL-Passwörter enthalten den unveränderten Rohwert. Im
Passwortanteil der korrespondierenden DSNs muss derselbe Wert URL-percent-encoded
sein, sobald er reservierte URI-Zeichen enthält.

Bei der Umstellung einer bestehenden Installation müssen die bisherigen
Dateiinhalte unverändert in die entsprechenden Direktvariablen übernommen
werden. Insbesondere dürfen für ein vorhandenes PostgreSQL-Volume nicht beiläufig
neue Passwörter erzeugt werden; neue Werte erfordern den unten beschriebenen
Rotationsablauf.

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

## Entwicklung und Tests

```bash
python3 -m pip install --require-hashes --no-deps -r backend/requirements-test.lock
PYTHONPATH=backend python3 -m pytest -q backend/tests
pnpm --dir frontend install
pnpm --dir frontend test
pnpm --dir frontend build
```

Backend und Frontend besitzen eigene Dockerfiles. Direkte und transitive
Abhängigkeiten werden exakt gelockt (Python inklusive Distributions-Hashes) und
über CI gemeinsam mit Migration, Compose-Konfiguration und Images geprüft.
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
Nach Änderung der vier Passwortvariablen müssen auch die vier DSN-Variablen
denselben neuen Stand enthalten. Anschließend werden die Backend-Verbindungen
angehalten, PostgreSQL mit den aktuellen Umgebungswerten neu erstellt und der
idempotente Hook explizit erneut ausgeführt:

```bash
docker compose stop api scheduler worker notification-worker
docker compose up -d --no-deps --force-recreate postgres
docker compose exec -T postgres sh /docker-entrypoint-initdb.d/001-create-app-role.sh
docker compose run --rm migrate
docker compose up -d --force-recreate api scheduler worker notification-worker
```

Der Hook aktualisiert Owner, API, Worker und Admin per `ALTER ROLE`, liest alle
Passwörter aus der Container-Umgebung und schreibt sie weder in Argumentlisten
noch Logs. Das anschließende Recreate verwirft offene
Connection-Pools mit alten Zugangsdaten. Derselbe Ablauf ist vor dem ersten
Upgrade auf Revision `0008` bei einem bestehenden Datenvolume erforderlich.
Auch bei jeder Rotation gilt: Passwortvariablen enthalten den Rohwert; der
Passwortanteil der korrespondierenden DSN-Variablen ist URL-percent-encoded.

Für eine Rotation des AES-GCM-Masterschlüssels:

1. Datenbank sichern und API, Scheduler sowie beide Worker stoppen.
2. Den alten Schlüssel als JSON unter seiner Versionsnummer in
   `WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS` eintragen,
   `WT_SYNC_ENCRYPTION_KEY_VERSION` erhöhen und
   `WT_SYNC_ENCRYPTION_SECRET` ersetzen.
3. Einmalig den Rotationslauf ausführen:

   ```bash
   docker compose run --rm notification-worker python -m app.rotate_encryption
   ```

4. Dienste starten und Provider, TOTP sowie Push prüfen. Erst danach den alten
   Schlüssel aus dem Keyring entfernen und die Container erneut erstellen.

Der Rotationslauf ist transaktional. Er verschlüsselt Provider-Zugänge,
TOTP-Secrets, Push-Abonnements und noch aufbewahrte Outbox-Nachrichten neu.
