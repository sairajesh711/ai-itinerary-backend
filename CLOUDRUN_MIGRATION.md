# Migration from Render to Google Cloud Run

## Why Cloud Run?
- **Cost**: Pay per request, scale to zero when idle
- **Reliability**: No random shutdowns like Render
- **Performance**: Faster cold starts, better timeout handling
- **Scalability**: Auto-scales based on traffic

## Cost Comparison
- **Render Starter**: $7/month (always running)
- **Cloud Run**: ~$1-3/month for low traffic (pay per use)

## Migration Steps

### 1. Setup Google Cloud Project
```bash
# Install gcloud CLI
# https://cloud.google.com/sdk/docs/install

# Login and create project
gcloud auth login
gcloud projects create your-project-id
gcloud config set project your-project-id

# Enable required APIs
gcloud services enable run.googleapis.com
gcloud services enable containerregistry.googleapis.com
gcloud services enable secretmanager.googleapis.com
```

### 2. Create Secrets
```bash
# Set your OpenAI API key
gcloud secrets create openai-api-key
echo "your-actual-openai-key" | gcloud secrets versions add openai-api-key --data-file=-

# Set allowed frontend origins
gcloud secrets create frontend-origins
echo "https://ai-itinerary-frontend.vercel.app" | gcloud secrets versions add frontend-origins --data-file=-
```

### 3. Deploy
```bash
# Make deploy script executable
chmod +x deploy.sh

# Deploy (replace with your project ID)
./deploy.sh your-project-id us-central1
```

### 4. Update Frontend
Update your frontend API base URL to the Cloud Run service URL (provided after deployment).

### 5. Cost Optimization Settings

#### Current Configuration (Ultra Low Cost)
- **min-instances**: 0 (scale to zero when idle)
- **max-instances**: 10 (prevent runaway scaling)
- **memory**: 512Mi (sufficient for FastAPI)
- **cpu**: 1 (adequate performance)
- **concurrency**: 100 (handle multiple requests per instance)
- **timeout**: 300s (5 minutes for long itinerary generation)

#### Production Optimization (after validating usage)
```bash
# Reduce cold starts with min-instances: 1
gcloud run services update ai-itinerary-backend \
  --min-instances 1 \
  --region us-central1
```

## Expected Costs

### Development/Testing (current settings)
- **Requests**: 1000/month → ~$0.40
- **CPU time**: 10 minutes → ~$0.50
- **Memory**: minimal → ~$0.10
- **Total**: ~$1-2/month

### Production (with min-instances: 1)
- **Always-on instance**: ~$7/month
- **Additional requests**: scale as needed
- **Total**: ~$7-15/month (still cheaper than Render Pro)

## Monitoring & Logs
```bash
# View logs
gcloud logging read "resource.type=cloud_run_revision"

# View metrics in Cloud Console
# https://console.cloud.google.com/run
```

## Rollback Plan
If issues occur, you can quickly redeploy to Render using existing `render.yaml` while debugging Cloud Run setup.

## GitHub Actions (Optional)
The included `.github/workflows/deploy-cloudrun.yml` enables automatic deployment on push to master.

Required secrets in GitHub:
- `GCP_PROJECT_ID`: Your Google Cloud project ID
- `GCP_SA_KEY`: Service account key JSON (base64 encoded)

## Benefits Over Render
1. **No random shutdowns** - Cloud Run is enterprise-grade
2. **Better timeout handling** - 300s vs Render's limitations
3. **True pay-per-use** - Only pay when requests are processed
4. **Faster cold starts** - Typically <2 seconds vs Render's 30+ seconds
5. **Better logging** - Integrated with Google Cloud Logging