# Zambia Election Live — ECZ Results Dashboard

A small Flask application that fetches the **official Electoral Commission of Zambia (ECZ) results portal** and presents a mobile-friendly presidential results dashboard.

## Official source

https://results.elections.org.zm/

The app intentionally does **not** generate projections, estimates, rumours or unofficial results.

## Update design

- Server fetches the ECZ portal every 30 minutes.
- Browser polls this app every 60 seconds.
- This means the ECZ server is not hit every minute.
- If the ECZ source cannot be fetched, the app displays the last known state/error rather than inventing numbers.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

## Production

Use a WSGI server such as Gunicorn, with the included config:

```bash
gunicorn app:app --config gunicorn.conf.py
```

`gunicorn.conf.py` starts the background fetch scheduler from a `post_fork`
hook so it only runs inside the worker process that actually serves
requests (gunicorn's master process also imports the app to validate it,
which would otherwise start a second, unused scheduler there).

Set `FETCH_MINUTES=30` if you want to make the interval explicit.

Deploying to Render: push this repo and use `render.yaml` (Blueprint deploy)
or set the start command above manually.

## Important

The ECZ portal is the authoritative source. Its HTML structure can change. If ECZ changes its page markup, update `fetch_results()` in `app.py` so that the parser matches the new official structure.

For a high-stakes public deployment, add:
- persistent database snapshots,
- source timestamps,
- an audit log,
- checksum/hash of fetched source data,
- retry/backoff,
- monitoring,
- HTTPS,
- rate-limit protection,
- and an explicit disclaimer that the app is an independent presentation layer and not ECZ.
