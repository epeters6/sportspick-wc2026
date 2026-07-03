"""
pavlov-weather-bot pipeline package.

Modules:
    kalshi_client   – Kalshi REST API wrapper (auth, markets, orders)
    nws_client      – NOAA/NWS REST API wrapper (forecasts, observations)
    station_mapper  – Maps Kalshi markets → NWS weather stations
    signal_engine   – Compares forecast probability vs market price → edge
    discord_bot     – Discord bot for alerts and manual commands
    order_manager   – Kelly-sized order placement and position tracking
    learning_loop   – Scores stations over time, updates station_scores.json
"""
