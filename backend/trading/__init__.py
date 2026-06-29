"""
Polymarket autobet system.

A risk-managed engine that compares our crowd-consensus probabilities against
live Polymarket prices, sizes positions with fractional Kelly, and (optionally)
executes trades on the Polymarket CLOB.

SAFETY MODEL
------------
The system defaults to *paper mode*: it records what it WOULD bet using real
market prices, but places no real orders. Live execution requires:
  1. POLYMARKET_LIVE_ENABLED=true in the environment, AND
  2. Valid CLOB credentials (private key + API key/secret/passphrase).
Even in live mode, every order passes through the full risk gate in risk.py.
"""
