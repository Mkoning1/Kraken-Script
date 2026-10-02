# Kraken multi-agent trader

Een team van agents dat de Kraken-markt volgt en met **echt geld** handelt. Draait op GitHub Actions,
aangestuurd door een externe wekker (cron-job.org) die elk kwartier een run start.
Dashboard: de GitHub Pages-site van deze repository (Handelsvloer en Details).

## Het team

| Agent | Taak |
|---|---|
| Data-agent | Kiest elke run de 15 EUR-munten met het meeste volume (plus BTC, ETH en alles waar je in zit) en haalt uur- en 4-uurscandles op. |
| Markt-agent | Stijgende, neutrale of dalende markt, op basis van BTC op 4 uur. |
| **Trend-4u** | Je oorspronkelijke bot: koopt een 4-uursuitbraak boven het hoogste punt van de vorige periode (RSI boven 50). Verkoopt via de tweetraps trailing stop. Gemiste 4-uurscandles worden alsnog verwerkt. |
| Dip-koper | Koopt paniekdalingen op uurcandles in een stijgende munt, verkoopt bij herstel of na 12 uur. |
| Uitbraak-agent | Koopt uitbraken boven het hoogste punt van de laatste 24 uur met extra volume. |
| Momentum-agent | Koopt een versnelling (MACD kruist omhoog met volume) op uurcandles. |
| Squeeze-agent | Koopt de uitbraak na een zeer rustige periode op uurcandles. |
| Prestatie-agent | Meet elke agent in R en bepaalt zijn invloed en of hij echt geld krijgt (zie promotie). |
| Beslis-agent | Rangschikt alle kansen: sterkte x invloed, bonus als agents het eens zijn, x marktfase. |
| Risico-agent | Inzet en limieten: daglimiet, noodstop, max posities, en halve inzet als een munt sterk meebeweegt met een munt waar je al in zit. |
| Uitvoer-agents | Echt geld via Kraken (met stop-loss order op Kraken zelf), schaduwgeld op papier. |
| Monitor-agent | Verslag in gewone taal en het dashboard. |
| Historie- en backtest-lab | Speelt twee jaar historie na en zoekt per agent betere instellingen (zie backtest). |

## Risicoprofiel

Standaard: `assertief`. Assertief op de markt, met een vangnet zodat je nooit alles kwijt kunt.

| | voorzichtig | normaal | **assertief** | agressief |
|---|---|---|---|---|
| Risico per trade | 1% | 1,5% | 2% | 2,5% |
| Grootte per positie | 25% | 35% | 40% | 50% |
| Max posities | 2 | 3 | 3 | 3 |
| Max belegd | 50% | 75% | 90% | 100% |
| Daglimiet verlies | 3% | 5% | 5% | 8% |
| Noodstop (alles verkopen) | 15% | 20% | 20% | 30% |

Nooit geleend geld. De noodstop gaat af als het vermogen 20% onder de hoogste stand komt; daarna wordt niet meer
gehandeld tot jij `data/state.json` verwijdert (open posities dan eerst zelf verkopen).

## Backtest en optimalisatie

Elke zondagnacht (of handmatig via Actions, Backtest, Run workflow):
1. De historie van de laatste twee jaar wordt bijgewerkt (uurcandles, opgeslagen in `data/history`).
2. Elke agent speelt die periode na met dezelfde regels, kosten (0,40%) en een ruime slippage (0,15%).
3. De eerste 60% wordt gebruikt om per agent een paar instellingen te proberen. De beste wordt getest op de laatste 40%,
   die hij nooit gezien heeft. Alleen als hij daar ook winst maakt, worden de nieuwe instellingen gebruikt (`data/tuned_params.json`).

Resultaten staan onder Details en in het dossier van elke agent op de handelsvloer.

### Promotie naar echt geld
- Backtest positief (minstens +0,1R per trade in de controleperiode): echt geld na 15 schaduwtrades met minstens +0,1R.
- Backtest negatief: blijft in de schaduw.
- Geen of te weinig backtest: echt geld na 40 schaduwtrades met minstens +0,2R.
- Met echt geld onder -0,25R over 10 trades: terug naar de schaduw.

## Veiligheid
- Vóór elke run draait `tests/quick.py`: de bot handelt een paar dagen tegen een nagebootste Kraken. Klopt er iets niet,
  dan wordt die run niet gehandeld. Je posities blijven beschermd door de stop-loss orders op Kraken.
- Nooit twee runs tegelijk; na elke echte order wordt direct opgeslagen; elke run vergelijkt het boek met je account.

## De wekker (cron-job.org)
GitHub voert geplande taken maar een paar keer per dag uit. Daarom start cron-job.org elk kwartier een run via de GitHub API:
- URL: `https://api.github.com/repos/Mkoning1/Kraken-Script/actions/workflows/trading-bot.yml/dispatches`
- Methode: POST, body: `{"ref":"main"}`
- Headers: `Accept: application/vnd.github+json` en `Authorization: Bearer <token>`
- Token: fine-grained, alleen deze repository, alleen Actions op Read and write.

## Bediening (`config.json`)

| Wat | Hoe |
|---|---|
| Niets echt uitvoeren, alleen laten controleren | `live.validate_only: true` |
| Geen nieuwe aankopen, wel posities bewaken | `live.trading_enabled: false` |
| Ander risicoprofiel | `profile` |
| Kopen in een dalende markt | `regime_multiplier.dalend` bijv. op `0.5` |
| Alle agents direct met echt geld | `promotion.all_agents_live: true` |
| Geoptimaliseerde instellingen uitzetten | `backtest.use_tuned_params: false` |
| Alles stoppen | Pauzeer de taak op cron-job.org en zet de workflow uit onder Actions. Open posities houden hun stop-loss op Kraken. |

## Kraken API-sleutel
GitHub Secrets `KRAKEN_API_KEY` en `KRAKEN_API_SECRET`, met de rechten Query Funds, Query Open Orders & Trades,
Query Closed Orders & Trades, Create & Modify Orders en Cancel & Close Orders. **Nooit** Withdraw Funds.
