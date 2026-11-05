# Kalshi weather analyzer

Twice a day (about 7 AM and 7 PM Eastern) a GitHub Actions job:

1. Finds every open Kalshi daily high/low temperature market (all cities).
2. Pulls ~140 ensemble forecast members (GFS, ECMWF, ICON, GEM via Open-Meteo), the official
   NWS forecast for each settlement station, and, for same-day markets, what the station has
   already recorded.
3. Turns that into a probability for every bracket and compares it with Kalshi's price after fees.
4. Writes the best opportunities to `results/latest.json` and logs them in `results/picks.csv`,
   then grades past picks once Kalshi settles them.

A Claude scheduled task reads the results, does extra research on the top picks, updates the
dashboard and sends a push notification.

**Not financial advice.** The model has no proven edge until `picks.csv` has 100+ settled
picks. Use limit orders, small stakes, and confirm the market's settlement station in its rules.

Settings: `config.json` (bankroll, minimum edge, price limits, models).
Run manually: Actions tab → "Kalshi weather analysis" → Run workflow.
