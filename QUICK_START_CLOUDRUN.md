# Quick Start: Google Cloud Run Migration

## 🚀 3-Step Setup (Mumbai Region)

### Prerequisites
```bash
# Install gcloud CLI and login
gcloud auth login
gcloud auth application-default login
```

### Step 1: Setup Project
```bash
chmod +x *.sh
./setup-gcp.sh your-project-id
```

### Step 2: Create Secrets
```bash
./create-secrets.sh
# Enter your OpenAI API key and frontend domain(s)
```

### Step 3: Deploy
```bash
./deploy-mumbai.sh
# Get your service URL: https://ai-itinerary-backend-xxx-el.a.run.app
```

## 🧪 Testing
```bash
./test-cors.sh https://your-service-url.run.app https://your-frontend.vercel.app
```

## 💰 Cost Settings

**Current (Reliability-focused):**
- Min instances: 1 (₹520/month)
- Always warm, no cold starts
- Best for production use

**Ultra-low cost option:**
```bash
gcloud run services update ai-itinerary-backend \
  --min-instances 0 \
  --region asia-south1
```
- Scales to zero when idle
- ~₹50-150/month for low traffic
- 2-3 second cold starts

## 🔄 Update Frontend
Set in Vercel environment:
```
VITE_API_BASE=https://your-service-url.run.app
```

## ✅ Benefits Over Render
- ❌ No random shutdowns
- ❌ No 15-second timeouts  
- ❌ No CORS failures
- ✅ Enterprise reliability
- ✅ Mumbai region = faster for India
- ✅ Pay-per-use pricing