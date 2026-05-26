# LoL Live Coach

A local League of Legends matchup coach that:

- **Fires once when you load into a game.** Polls Riot's local Live Client Data API
  (`https://127.0.0.1:2999/liveclientdata/allgamedata`, no key required, only reachable
  while you're actually in a match). As soon as both teams are confirmed, it captures
  the matchup and asks Claude for itemization advice — then sits still. It does not
  re-query during the game and does not show live K/D/items/gold. Open it once, glance
  at the build / counter items / tips, close it (or leave it open as a reference) and
  play.
- **Sends the matchup to Claude** via the Anthropic SDK with the `web_search` tool, so
  Claude can pull current-patch builds, item priorities, and trait counters from sites
  like u.gg / op.gg / mobalytics before answering. The system prompt forbids claims
  about specific ability mechanics — counter items must be tied to stat-level traits
  (healing, heavy AD, heavy AP, burst, hard CC, mobility, attack speed, shielding).
- **Auto-syncs a local patch database** of champions, items, runes, and summoner spells
  from Data Dragon (`ddragon.leagueoflegends.com`). Checks for new patches in the
  background and re-downloads when one ships.
- **Hosts a local dashboard** at `http://127.0.0.1:8765` with a glanceable view of best
  items, counter items, and tips, plus a settings page to configure your Claude API key.
- **Logs everything for debugging.** A complete trace of each Claude call (full request,
  web-search results, response, parsed advice) is appended to `~/.lolstats/logs/claude.jsonl`
  — share that file when advice looks off.

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
- The advisor is keyed on the matchup fingerprint (your champion + role + the
  champion/role pairs on each team). That value stays constant for the whole
  match, so Claude is called exactly once per game. If you want to re-query (e.g.
  the first call returned an error), use the **↻ Re-query** button — it clears
  the cache and asks again with the same matchup.
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
