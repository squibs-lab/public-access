# Public Access

International public access TV: flip through live, unperformed channels from around the world — church services, zoo cams, harbors, council meetings, trains.

## How it works
- `channels.csv` is the dial. Anything marked `n` in the `approved` column is off the air.
- `tv.py --once` checks which channels are live (YouTube API + channel RSS feeds) and writes `live.json`.
- `.github/workflows/update-live.yml` runs that every 20 minutes and publishes `index.html`, `privacy.html` and `live.json` to GitHub Pages. It needs one repository secret, `YT_API_KEY`.

## Watching locally (with the reject key)
```
python tv.py          # opens http://localhost:8000
```
Put your API key in `key.txt` (never committed). On your own machine, **X** rejects the current channel (marks it `n` in `channels.csv`) and **U** undoes. Push the updated `channels.csv` to update the public dial.
`fullscreen.html` is an edge-to-edge local-only view.

## Finding more channels
```
python seed_channels.py --key YOUR_KEY
```
Adds newly found live channels to `channels.csv` for review.
