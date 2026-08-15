# Telegram Gift GIF

A web renderer for Telegram collectible gifts. Paste a public `t.me/nft/...` URL and receive a looping **420×420 GIF** with a Telegram iOS-inspired card layout.

[Open the live app](http://205.196.80.135:27643)

## Features

- Reads Telegram's public collectible metadata and Lottie animation.
- Renders backdrop, gift model, and the gift-specific pattern separately.
- Uses each collectible's own symbol in the calibrated 17-point layout, with darker symbols and vertical-only edge fading.
- Adds a sharp ribbon, rounded envelope, and geometry-linked collectible number.
- Pre-renders opaque RGB PNG frames with a cached static 4x card base, then assembles a looping GIF with a shared animation palette; also exports metadata and a debug sheet.

## Run with Docker

```bash
docker compose up --build
```

Open <http://localhost:8000> and paste a Telegram collectible URL.

## Run with Python

Python 3.12 is recommended. The native `rlottie-python` package must be available for your platform.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Health check: <http://localhost:8000/api/health>

## Deploy

The repository includes a `Dockerfile` and `render.yaml`. Deploy it on any Docker host or create a Render Blueprint from this repository.

> GitHub Pages alone cannot run this application because rendering requires Python and native rlottie. GitHub hosts the public source; the included container runs the working site.

## Font note

Production uses **SF Mono Semibold** for the collectible number. Apple’s font is not redistributed here. The public build automatically falls back to DejaVu Sans Mono. To use your own licensed copy, set `NUMBER_FONT_PATH`.

## API

```bash
curl -X POST http://localhost:8000/api/render \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://t.me/nft/SnoopCigar-99451"}'
```

## License and attribution

Original project code is MIT-licensed. The ribbon asset is derived from Telegram iOS artwork; see `THIRD_PARTY_NOTICES.md` for attribution and upstream licensing.
