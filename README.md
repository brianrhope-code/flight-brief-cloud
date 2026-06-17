# Flight Brief Cloud

Cloud-hosted pilot brief builder for iPhone/iPad.

Upload a flight plan, trip kit, and pairing PDF, then generate:

- Text brief
- Card PDF
- Full brief PDF
- Landscape synopsis
- Pickup/report time from pairing
- Flight timeline data

## Local cloud-mode test

```bash
python -m pip install -r requirements.txt
FLIGHT_BRIEF_CLOUD_MODE=true FLIGHT_BRIEF_OUTPUT_DIR=/tmp/flight-brief-output PORT=8777 python desktop_flight_brief_server.py
```

Open `http://127.0.0.1:8777`.

## Deploy

Use `render.yaml` with Render Blueprint deploy.
