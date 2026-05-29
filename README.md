# worshiptool-churchtools-sync

Sync von Worshiptools zu Churchtools  
Fügt Songs von Worshiptools Charts in Churchtools Event Agenda hinzu.

## Prepare

1. `cp .env.example .env` ENV ausfüllen
2. `config.yaml` konfigurieren

## Run

Docker:

```
docker run -v ./config.yaml:/config.yaml -v ./db.yaml:/db.yaml --env-file ./.env davidloe/worshiptools-churchtools-sync --config /config.yaml --db /db.yaml
```

Local:

```
python3 sync.py
```

```
options:
  -h, --help           show this help message and exit
  --loglevel LOGLEVEL  Setzt das Loglevel (DEBUG, INFO, WARNING, ERROR, CRITICAL)
  --config CONFIG      Pfad zur Konfigurationsdatei
  --db DB              Pfad zur Yaml DB Datei
```

## Tests

```
python3 -m pytest
```
