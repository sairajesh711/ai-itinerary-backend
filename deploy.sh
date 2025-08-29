#!/bin/bash

# Google Cloud Run Deployment Script
# Usage: ./deploy.sh [PROJECT_ID] [REGION]

set -e

PROJECT_ID=${1:-"your-project-id"}
REGION=${2:-"us-central1"}
SERVICE_NAME="ai-itinerary-backend"
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "🚀 Deploying to Google Cloud Run..."
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Service: ${SERVICE_NAME}"

# Build and push image
echo "📦 Building Docker image..."
docker build -t ${IMAGE_NAME} .

echo "⬆️  Pushing to Google Container Registry..."
docker push ${IMAGE_NAME}

# Create secrets (run once)
echo "🔐 Creating secrets..."
gcloud secrets create openai-api-key --project=${PROJECT_ID} --quiet || echo "Secret openai-api-key already exists"
gcloud secrets create frontend-origins --project=${PROJECT_ID} --quiet || echo "Secret frontend-origins already exists"

# Deploy to Cloud Run
echo "☁️  Deploying to Cloud Run..."
gcloud run deploy ${SERVICE_NAME} \
  --image ${IMAGE_NAME} \
  --platform managed \
  --region ${REGION} \
  --project ${PROJECT_ID} \
  --allow-unauthenticated \
  --port 8000 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10 \
  --concurrency 100 \
  --timeout 300 \
  --set-env-vars="PORT=8000,APP_ENV=production,DEBUG=false,HOST=0.0.0.0,OPENAI_MODEL=gpt-4o,DEFAULT_CURRENCY=USD,LOG_LEVEL=INFO,SECURITY_ENABLED=true,MAX_REQUEST_SIZE_KB=50,RATE_LIMIT_REQUESTS=15,RATE_LIMIT_WINDOW_SECONDS=300" \
  --set-secrets="OPENAI_API_KEY=openai-api-key:latest,FRONTEND_ORIGINS=frontend-origins:latest"

echo "✅ Deployment complete!"

# Get service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} --region=${REGION} --project=${PROJECT_ID} --format="value(status.url)")
echo "🌐 Service URL: ${SERVICE_URL}"
echo ""
echo "Next steps:"
echo "1. Set your secrets:"
echo "   gcloud secrets versions add openai-api-key --data-file=- <<< 'your-openai-key'"
echo "   gcloud secrets versions add frontend-origins --data-file=- <<< 'https://yourdomain.com'"
echo "2. Update your frontend to use: ${SERVICE_URL}"
echo "3. Test: curl ${SERVICE_URL}/health"