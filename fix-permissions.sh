#!/bin/bash

# Fix Cloud Run permissions for Secret Manager access
set -e

PROJECT_ID="ai-itinerary-backend"
PROJECT_NUMBER="961837492888"
SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "🔐 Granting Secret Manager permissions to Cloud Run service account..."
echo "Project: $PROJECT_ID"
echo "Service Account: $SERVICE_ACCOUNT"
echo ""

# Grant Secret Manager Secret Accessor role at project level
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role="roles/secretmanager.secretAccessor"

echo "✅ Permissions granted!"
echo ""
echo "Now run: ./deploy-mumbai.sh"