#!/bin/bash

# Create secrets in Google Secret Manager
# Usage: ./create-secrets.sh

set -e

echo "🔐 Creating secrets in Google Secret Manager..."

# Check if project is configured
PROJECT_ID=$(gcloud config get project)
if [ -z "$PROJECT_ID" ]; then
    echo "❌ No project configured. Run ./setup-gcp.sh first"
    exit 1
fi

echo "Project: $PROJECT_ID"
echo ""

# Create OpenAI API Key secret
echo "📝 Setting up OPENAI_API_KEY secret..."
if gcloud secrets describe openai-api-key &>/dev/null; then
    echo "Secret 'openai-api-key' already exists"
else
    gcloud secrets create openai-api-key --replication-policy="automatic"
    echo "✅ Created 'openai-api-key' secret"
fi

echo "Please enter your OpenAI API key:"
read -s -p "OPENAI_API_KEY: " OPENAI_KEY
echo ""

if [ -n "$OPENAI_KEY" ]; then
    echo -n "$OPENAI_KEY" | gcloud secrets versions add openai-api-key --data-file=-
    echo "✅ OpenAI API key stored"
else
    echo "⚠️  No OpenAI API key provided - you'll need to set it later"
fi

# Create Frontend Origins secret
echo ""
echo "📝 Setting up FRONTEND_ORIGINS secret..."
if gcloud secrets describe frontend-origins &>/dev/null; then
    echo "Secret 'frontend-origins' already exists"
else
    gcloud secrets create frontend-origins --replication-policy="automatic"
    echo "✅ Created 'frontend-origins' secret"
fi

echo "Enter your frontend domain(s) (comma-separated):"
echo "Example: https://ai-itinerary-frontend.vercel.app,https://yourdomain.com"
read -p "FRONTEND_ORIGINS: " FRONTEND_ORIGINS

if [ -n "$FRONTEND_ORIGINS" ]; then
    echo -n "$FRONTEND_ORIGINS" | gcloud secrets versions add frontend-origins --data-file=-
    echo "✅ Frontend origins stored"
else
    # Set default for development
    DEFAULT_ORIGINS="https://ai-itinerary-frontend.vercel.app,http://localhost:5173,http://127.0.0.1:5173"
    echo -n "$DEFAULT_ORIGINS" | gcloud secrets versions add frontend-origins --data-file=-
    echo "✅ Default frontend origins stored: $DEFAULT_ORIGINS"
fi

echo ""
echo "🎉 Secrets created successfully!"
echo ""
echo "You can update them later with:"
echo "  echo 'new-key' | gcloud secrets versions add openai-api-key --data-file=-"
echo "  echo 'new-origins' | gcloud secrets versions add frontend-origins --data-file=-"
echo ""
echo "Next step: Run ./deploy-mumbai.sh"