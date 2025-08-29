#!/bin/bash

# Test CORS configuration for Google Cloud Run deployment
# Usage: ./test-cors.sh [SERVICE_URL] [FRONTEND_DOMAIN]

SERVICE_URL=${1:-"https://ai-itinerary-backend-XXXX-el.a.run.app"}
FRONTEND_DOMAIN=${2:-"https://ai-itinerary-frontend.vercel.app"}

if [[ "$SERVICE_URL" == *"XXXX"* ]]; then
    echo "❌ Please provide your actual Cloud Run service URL"
    echo "Usage: ./test-cors.sh https://your-service-url.run.app https://your-frontend.vercel.app"
    exit 1
fi

echo "🧪 Testing CORS configuration..."
echo "Service URL: $SERVICE_URL"
echo "Frontend Domain: $FRONTEND_DOMAIN"
echo ""

# Test 1: Health check (simple GET)
echo "1️⃣ Testing health endpoint..."
curl -s -w "Status: %{http_code}\n" "$SERVICE_URL/health" | head -1
echo ""

# Test 2: CORS debug endpoint
echo "2️⃣ Testing CORS configuration..."
curl -s "$SERVICE_URL/debug/cors" | python -m json.tool
echo ""

# Test 3: OPTIONS preflight request
echo "3️⃣ Testing CORS preflight (OPTIONS)..."
curl -i -X OPTIONS "$SERVICE_URL/jobs/itinerary" \
  -H "Origin: $FRONTEND_DOMAIN" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  -s -w "\n--- Response Status: %{http_code} ---\n"
echo ""

# Test 4: Actual POST request with CORS headers
echo "4️⃣ Testing actual POST request with CORS..."
curl -i -X POST "$SERVICE_URL/jobs/itinerary" \
  -H "Origin: $FRONTEND_DOMAIN" \
  -H "Content-Type: application/json" \
  -d '{
    "destination": "Mumbai",
    "start_date": "2025-10-01", 
    "duration_days": 3,
    "budget_level": "mid_range",
    "home_currency": "INR"
  }' \
  -s -w "\n--- Response Status: %{http_code} ---\n"

echo ""
echo "✅ CORS test complete!"
echo ""
echo "🔍 What to look for:"
echo "  • Preflight should return 200 with Access-Control-Allow-* headers"
echo "  • POST should return Access-Control-Allow-Origin header"
echo "  • No 'blocked by CORS policy' errors"