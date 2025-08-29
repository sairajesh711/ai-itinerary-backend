#!/bin/bash

# Deploy AI Itinerary Backend to Google Cloud Run (Mumbai)
# Optimized for Indian users with cost-efficient settings

set -e

PROJECT_ID=$(gcloud config get project)
SERVICE_NAME="ai-itinerary-backend"
REGION="asia-south1"  # Mumbai
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

if [ -z "$PROJECT_ID" ]; then
    echo "❌ No project configured. Run ./setup-gcp.sh first"
    exit 1
fi

echo "🚀 Deploying AI Itinerary Backend to Cloud Run (Mumbai)..."
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION} (Mumbai)"
echo "Service: ${SERVICE_NAME}"
echo ""

# Build and push image using Cloud Build (faster than local Docker)
echo "🏗️  Building image with Cloud Build..."
gcloud builds submit --tag ${IMAGE_NAME}:latest --timeout=600s

echo "☁️  Deploying to Cloud Run..."
gcloud run deploy ${SERVICE_NAME} \
  --image ${IMAGE_NAME}:latest \
  --platform managed \
  --region ${REGION} \
  --allow-unauthenticated \
  --port 8000 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 10 \
  --concurrency 10 \
  --timeout 900 \
  --execution-environment gen2 \
  --cpu-throttling \
  --set-env-vars="APP_ENV=production,DEBUG=false,HOST=0.0.0.0,OPENAI_MODEL=gpt-4o,DEFAULT_CURRENCY=INR,LOG_LEVEL=INFO,SECURITY_ENABLED=true,MAX_REQUEST_SIZE_KB=50,RATE_LIMIT_REQUESTS=15,RATE_LIMIT_WINDOW_SECONDS=300" \
  --set-secrets="OPENAI_API_KEY=openai-api-key:latest,FRONTEND_ORIGINS=frontend-origins:latest"

# Get service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} --region=${REGION} --format="value(status.url)")

echo ""
echo "🎉 Deployment successful!"
echo "🌐 Service URL: ${SERVICE_URL}"
echo ""
echo "📋 Configuration Summary:"
echo "  • Region: Asia South 1 (Mumbai) - Best for Indian users"
echo "  • Min Instances: 1 (keeps one warm - smooth UX)"
echo "  • Max Instances: 10 (handles traffic spikes)"
echo "  • Memory: 1GB (sufficient for OpenAI API calls)"
echo "  • CPU: 1 vCPU (good performance)"
echo "  • Concurrency: 10 (optimal for AI workload)"
echo "  • Timeout: 15 minutes (enough for complex itineraries)"
echo "  • Default Currency: INR (Indian Rupee)"
echo ""
echo "🧪 Testing endpoints..."
echo "Health check:"
curl -s "${SERVICE_URL}/health" | python -m json.tool

echo ""
echo "CORS debug:"
curl -s "${SERVICE_URL}/debug/cors" | python -m json.tool

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Next steps:"
echo "1. Update your frontend environment variable:"
echo "   VITE_API_BASE=${SERVICE_URL}"
echo ""
echo "2. Test CORS with your frontend domain:"
echo "   curl -i -X OPTIONS \"${SERVICE_URL}/jobs/itinerary\" \\"
echo "     -H \"Origin: https://your-frontend.vercel.app\" \\"
echo "     -H \"Access-Control-Request-Method: POST\""
echo ""
echo "3. Monitor logs:"
echo "   gcloud logging read \"resource.type=cloud_run_revision\" --limit 50"
echo ""
echo "💰 Cost optimization notes:"
echo "  • Min-instances: 1 = ~₹520/month for always-on instance"
echo "  • Scale to 0: Change min-instances to 0 to save costs during low usage"
echo "  • Current setup optimized for reliability over cost"