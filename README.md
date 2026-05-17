# LoL Live Coach

A local League of Legends coach that:

- **Auto-detects when you enter a game** by polling Riot's local Live Client Data API
  (`https://127.0.0.1:2999/liveclientdata/allgamedata`, no key required, only available
  while a match is running).
- **Sends the game state to Claude** via the Anthropic SDK with the `web_search`
  tool, so Claude can look up the current patch's builds, counters, and matchups
  before answering.
- **Auto-syncs a local patch database** of champions, items, runes, and summoner spells
  from Data Dragon (`ddragon.leagueoflegends.com`). It checks for new patches in the
  background and re-downloads when one ships.
- **Hosts a local dashboard** at `http://127.0.0.1:8765` with a glanceable view of best
  items, counter items, and tips, plus a settings page to configure your Claude API key.

The Anthropic key, model choice, polling cadence, and advisor cooldown are all
configurable from the web UI. Settings are stored in `~/.lolstats/config.json` and
patch data in `~/.lolstats/patch_data.sqlite`.

## Setup

```bash
pip install -r requirements.txt
python run.py
```

Then open <http://127.0.0.1:8765> (the script tries to open it for you), go to
**Settings**, paste your Anthropic API key, and save. The dashboard updates over
WebSocket as soon as a match starts.

## Notes

- The Live Client API only listens while a match is being played. Before that, the
  dashboard shows "Waiting for a game…". The League client uses a self-signed
  certificate; we disable verification because the endpoint is loopback-only.
- Patch data downloads the first time you run the app; subsequent starts reuse the
  SQLite cache. Use the **Re-download current patch** button to force a refresh.
- Claude requests are cached by a state fingerprint (champ picks + item builds +
  rough game phase), so the same game state doesn't burn budget repeatedly. There's
  also a configurable cooldown between requests.
- Default model is `claude-opus-4-7`. Switch to Sonnet or Haiku in Settings if you
  want lower cost.

## Layout

```
lolstats/
├── main.py              # FastAPI app, WebSocket, REST
├── config.py            # ~/.lolstats/config.json wrapper
├── db.py                # SQLite schema + helpers
├── data_dragon.py       # Riot static-data CDN + patch watcher
├── live_client.py       # Local /liveclientdata/* client + state reducer
├── game_monitor.py      # Background poll loop
├── claude_advisor.py    # Anthropic SDK + web_search
└── static/              # HTML / CSS / JS dashboard
```
