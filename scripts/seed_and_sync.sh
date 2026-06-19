#!/usr/bin/env bash
# Run once after first start to seed influencers and do an initial sync.
BASE_URL="${1:-http://localhost:8000}"

echo "Seeding influencer accounts..."
curl -s -X POST "$BASE_URL/seed" | python3 -m json.tool

echo ""
echo "Syncing World Cup data & triggering first scrape..."
curl -s -X POST "$BASE_URL/sync" | python3 -m json.tool

echo ""
echo "✅ Done! Open $BASE_URL/docs to explore the API."
