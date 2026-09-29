# Kraken multi-agent trader

Een team van agents dat elke 15 minuten de Kraken-markt bekijkt en met **echt geld** handelt,
binnen een budget dat jij instelt. Draait op GitHub Actions. Dashboard: de GitHub Pages-site van deze repository.

## Hoe het werkt

| Agent | Taak |
|---|---|
| Data-agent | Kiest elke run de 15 EUR-munten met het meeste volume (plus BTC, ETH en alles waar je in zit) en haalt 15-minuten-, uur- en 4-uurscandles op. |
| Markt-agent | Stijgende, neutrale of dalende markt, op basis van BTC. |
| **Trend-4u** | Je oorspronkelijke bot: koopt als een 4-uursslot boven het hoogste punt van de vorige 55 candles breekt (RSI boven 50). Verkoopt via de tweetraps trailing stop: 2 ATR onder de piek, 6 ATR zodra de winst 3 ATR is. |
| Dip-koper | Koopt paniekdalingen in een stijgende munt, verkoopt bij herstel naar het gemiddelde of na 6 uur. |
| Uitbraak-agent | Koopt uitbraken op 15 minuten met extra volume. |
| Momentum-agent | Koopt een versnelling (MACD kruist omhoog met volume). |
| Squeeze-agent | Koopt de uitbraak na een zeer rustige periode. |
| Prestatie-agent | Meet elke agent in R (resultaat gedeeld door risico) en bepaalt zijn invloed en of hij echt geld krijgt. |
| Beslis-agent | Rangschikt alle kansen: sterkte x invloed, bonus als agents het eens zijn, x marktfase. |
| Risico-agent | Bepaalt de inzet en heeft het laatste woord: daglimiet, noodstop, max posities, budget. |
| Uitvoer-agents | Echt geld via Kraken, schaduwgeld op papier. Bewaken elke positie. |
| Monitor-agent | Schrijft per run een verslag in gewone taal en werkt het dashboard bij. |

### Echt geld en schaduwgeld
Trend-4u handelt vanaf de start met echt geld. De andere agents beginnen met schaduwgeld (â‚¬ 1.000 op papier).
Na 20 trades met gemiddeld minstens +0,2R per trade (na kosten) krijgen ze automatisch echt geld.
Een agent die met echt geld over 10 trades gemiddeld onder -0,25R zakt, gaat terug naar de schaduw.
Wil je alle agents direct met echt geld: zet `promotion.all_agents_live` op `true`.
E©n agent handmatig vastzetten: zet bij die agent `"force": "live"` of `"force": "schaduw"`.

### Bescherming van je geld
- **Budget:** de bot gebruikt nooit meer dan `live.budget_eur`, ook als er meer op je Kraken-account staat.
- **Stop-loss op Kraken:** elke positie heeft een stop-loss order op Kraken zelf. Hapert GitHub, dan beschermt Kraken je alsnog.
  Bij Trend-4u ligt die noodstop 1 ATR onder de gewone trailing stop, zodat korte uitschieters je runner niet wegschieten.
- **Daglimiet en noodstop:** meer dan 8% verlies op een dag: die dag niets nieuws. 30% onder de hoogste stand: alles verkopen en stoppen (profiel agressief).
- **Nooit twee runs tegelijk** en na elke order wordt direct opgeslagen.
- **Controle:** elke run vergelijkt de bot zijn posities met Kraken. Uitgevoerde stops en handmatige verkopen worden herkend.

## Bediening (alles in `config.json`)

| Wat | Hoe |
|---|---|
| Niets echt uitvoeren, alleen laten controleren | `live.validate_only: true` |
| Geen nieuwe aankopen, wel posities bewaken | `live.trading_enabled: false` |
| Budget verhogen | `live.budget_eur` hoger zetten (verlagen werkt niet; stop dan en begin opnieuw) |
| Minder agressief | `profile`: `normaal` of `voorzichtig` |
| Kopen in een dalende markt | `regime_multiplier.dalend` bijv. op `0.5` |
| Alles stoppen | Tabblad Actions, Trader, Disable workflow. Open posities houden hun stop-loss op Kraken. |
| Opnieuw beginnen na een noodstop | Verwijder `data/state.json` (open posities eerst zelf verkopen op Kraken) |

## Kraken API-sleutel
Nodig als GitHub Secrets `KRAKEN_API_KEY` en `KRAKEN_API_SECRET`. De sleutel moet deze rechten hebben:
Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades, Create & Modify Orders, Cancel & Close Orders.
**Nooit** Withdraw Funds. In validatiemodus controleert de bot zelf of de rechten kloppen en meldt dat op het dashboard.

## Kosten
Kraken rekent 0,40% per order, heen en terug ongeveer 1% inclusief slippage. Korte-termijnagents handelen vaak; daarom
moeten ze eerst in de schaduw bewijzen dat ze na kosten verdienen. Het dashboard toont de betaalde kosten per boek.

## Je oude bot
Bij de eerste run neemt de bot je open positie en tradehistorie uit `bot_state.json` over. Het oude script staat in `archief/`.
