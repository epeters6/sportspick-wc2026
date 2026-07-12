# Sports Shadow Validation Gates

The following checklist contains validation gates that MUST be cleared before any live trading is permitted for the sports quant models.

- [ ] Minimum 2–4 weeks of shadow/paper logs
- [ ] No live-order path reachable from shadow mode
- [ ] No missing order-book timestamps
- [ ] No assumed timestamps except explicitly flagged shadow rows
- [ ] Settlement mapping verified for MLB
- [ ] Team-market matching ambiguity rate acceptably low
- [ ] 15m and 1h CLV collected for most paper fills
- [ ] Positive or at least non-negative CLV by segment before scaling
- [ ] Calibration curve reviewed by probability bucket
- [ ] Brier score and log loss reviewed
- [ ] Fees/slippage included in all paper fills
- [ ] Default coefficients replaced or explicitly approved after out-of-sample testing
- [ ] Kalshi sports mapping verified before Kalshi routing is enabled
