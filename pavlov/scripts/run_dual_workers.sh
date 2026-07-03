#!/bin/sh
# Run Kalshi/weather bot (repo root) and MLB Polymarket bot (pavlov-mlb-bot) in one Railway worker.
#
# When STATE_DIRECTORY is set (e.g. Railway volume), MLB gets STATE_DIRECTORY/mlb_bot so
# logs/ and data/ do not overwrite the weather bot's positions.json.
#
# Discord: one gateway session per bot token. Set DISCORD_BOT_TOKEN (weather) and
# DISCORD_BOT_TOKEN_MLB (MLB). If DISCORD_BOT_TOKEN_MLB is unset, MLB uses DISCORD_BOT_TOKEN.
python main.py run &
# Stagger MLB Discord connect so both bots do not hammer discord.com/API
# from the same Railway egress IP at once (avoids Cloudflare 1015 rate limits).
sleep "${DISCORD_MLB_START_DELAY_SECONDS:-90}"
(
  cd pavlov-mlb-bot || exit 1
  if [ -n "${DISCORD_BOT_TOKEN_MLB:-}" ]; then
    export DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN_MLB}"
  fi
  if [ -n "${STATE_DIRECTORY:-}" ]; then
    export STATE_DIRECTORY="${STATE_DIRECTORY}/mlb_bot"
  fi
  exec python main.py run
) &
wait
