FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

WORKDIR /usr/src/app

COPY requirements-runtime.lock ./
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements-runtime.lock

COPY ./*.py .

ENTRYPOINT ["python", "sync.py"]
