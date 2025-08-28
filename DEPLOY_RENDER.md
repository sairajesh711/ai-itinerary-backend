# 🚀 Render Deployment Guide - AI Itinerary Backend

## 📋 Pre-Deployment Checklist

### ✅ **Critical Requirements**
- [ ] OpenAI API key ready (from https://platform.openai.com/api-keys)
- [ ] Frontend domain ready (e.g., `https://yourdomain.com`)
- [ ] GitHub repository ready for deployment
- [ ] All environment variables identified

---

## 🔧 **Step 1: Prepare Your Repository**

### **1.1 Environment Security**
```bash
# NEVER commit your .env file!
# Your .env should contain placeholder values only
```

**✅ Your `.env` file should look like this:**
```env
OPENAI_API_KEY=your-openai-api-key-here
FRONTEND_ORIGINS=https://yourdomain.com
```

### **1.2 File Structure Check**
```
ai_itinerary_backend/
├── main.py                 ✅ Entry point
├── requirements.txt        ✅ Dependencies  
├── render.yaml            ✅ Render config
├── Dockerfile             ✅ Container config (optional)
├── .env.production        ✅ Template
└── DEPLOY_RENDER.md       ✅ This guide
```

---

## 🌐 **Step 2: Deploy to Render**

### **2.1 Connect Repository**
1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click **"New"** → **"Web Service"**
3. Connect your GitHub repository
4. Select `ai_itinerary_backend` folder if in monorepo

### **2.2 Basic Configuration**
```yaml
Name: ai-itinerary-backend
Environment: Python
Region: Oregon (or closest to your users)
Branch: main
Build Command: pip install --upgrade pip && pip install -r requirements.txt
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
```

### **2.3 Environment Variables (CRITICAL)**

**⚠️ Set these in Render Dashboard > Environment:**

| Variable | Value | Notes |
|----------|-------|-------|
| `APP_ENV` | `production` | **Required** |
| `DEBUG` | `false` | **Required** |
| `OPENAI_API_KEY` | `sk-proj-YOUR_REAL_KEY` | **🔒 CRITICAL - Replace with actual key** |
| `FRONTEND_ORIGINS` | `https://yourdomain.com` | **🔒 CRITICAL - Your actual frontend domain** |
| `OPENAI_MODEL` | `gpt-4o-mini` | Optional |
| `DEFAULT_CURRENCY` | `USD` | Optional |
| `RATE_LIMIT_REQUESTS` | `15` | Optional |
| `RATE_LIMIT_WINDOW_SECONDS` | `300` | Optional |
| `LOG_LEVEL` | `INFO` | Optional |

### **2.4 Advanced Settings**
```yaml
Plan: Starter (upgrade to Standard for production load)
Health Check Path: /health
Auto-Deploy: Enabled
```

---

## 🔒 **Step 3: Security Configuration**

### **3.1 Production CORS Setup**
```bash
# In Render Dashboard, set FRONTEND_ORIGINS to:
https://yourdomain.com,https://www.yourdomain.com
```

### **3.2 API Key Security**
- ✅ **Never** commit API keys to Git
- ✅ Use Render's secure environment variables
- ✅ Rotate keys regularly
- ✅ Monitor usage in OpenAI dashboard

---

## 🧪 **Step 4: Test Your Deployment**

### **4.1 Health Check**
```bash
curl https://your-render-url.onrender.com/health
# Expected: {"status":"ok","openai_key_loaded":true,"model":"gpt-4o-mini"}
```

### **4.2 CORS Test**
```bash
curl -X OPTIONS https://your-render-url.onrender.com/jobs/itinerary \
  -H "Origin: https://yourdomain.com" \
  -H "Access-Control-Request-Method: POST"
# Expected: 200 OK with CORS headers
```

### **4.3 Security Test**
```bash
# Test rate limiting
for i in {1..10}; do
  curl -X POST https://your-render-url.onrender.com/jobs/itinerary \
    -H "Content-Type: application/json" \
    -d '{"destination":"Paris","start_date":"2024-12-01","duration_days":1}'
done
# Expected: First 15 succeed, rest get 429 rate limited
```

### **4.4 End-to-End Test**
```bash
# Create job
curl -X POST https://your-render-url.onrender.com/jobs/itinerary \
  -H "Content-Type: application/json" \
  -d '{
    "destination": "Tokyo",
    "start_date": "2024-12-15", 
    "duration_days": 2,
    "budget_level": "moderate",
    "home_currency": "USD",
    "max_daily_budget": 100
  }'
# Expected: {"job_id":"abc123","status":"running"}

# Check job status
curl https://your-render-url.onrender.com/jobs/abc123
# Expected: Job with "status":"done" and full itinerary
```

---

## 📊 **Step 5: Monitor Your Deployment**

### **5.1 Render Logs**
- Monitor logs in Render Dashboard
- Look for startup messages
- Watch for security events

### **5.2 Key Metrics to Watch**
```bash
# Startup logs should show:
INFO: AI Itinerary Backend starting
INFO: environment=production, security_features=enabled
INFO: CORS origins configured for production
```

### **5.3 Security Monitoring**
- Rate limit violations
- Prompt injection attempts
- CORS violations
- Oversized requests

---

## 🚨 **Troubleshooting**

### **Common Issues**

#### **Issue 1: "OPENAI_API_KEY must be set"**
```
Solution: Set OPENAI_API_KEY in Render environment variables
- Go to Dashboard > Your Service > Environment
- Add: OPENAI_API_KEY = sk-proj-YOUR_ACTUAL_KEY
```

#### **Issue 2: CORS Errors**
```
Solution: Set FRONTEND_ORIGINS correctly
- Add: FRONTEND_ORIGINS = https://yourdomain.com
- Remove any localhost URLs for production
```

#### **Issue 3: Build Failures**
```
Solution: Check requirements.txt
- Ensure all dependencies are specified
- Use specific versions for stability
```

#### **Issue 4: 500 Internal Server Error**
```
Solution: Check Render logs
- Look for Python tracebacks
- Common issues: missing environment variables
```

---

## 🔄 **Step 6: Post-Deployment**

### **6.1 Update Frontend**
Update your frontend API base URL:
```javascript
// Replace localhost with your Render URL
const API_BASE = 'https://your-render-url.onrender.com';
```

### **6.2 Domain Setup (Optional)**
1. In Render Dashboard → Settings → Custom Domains
2. Add your custom domain: `api.yourdomain.com`
3. Update DNS records as instructed
4. Update CORS settings to match

### **6.3 Performance Optimization**
```yaml
# For high traffic, upgrade to:
Plan: Standard or Pro
Instance Type: Optimized for your needs
```

---

## 🎯 **Production Checklist**

### **Before Going Live:**
- [ ] Environment variables set correctly
- [ ] Health check passes
- [ ] CORS configured for your domain
- [ ] Rate limiting tested
- [ ] Security features verified
- [ ] Frontend API URL updated
- [ ] OpenAI billing limits set
- [ ] Error monitoring in place

### **After Deployment:**
- [ ] Monitor logs for first 24 hours
- [ ] Test all API endpoints
- [ ] Verify rate limiting works
- [ ] Check OpenAI usage patterns
- [ ] Set up alerts for errors

---

## 📞 **Support**

### **If Issues Persist:**
1. **Check Render Logs**: Dashboard → Logs
2. **Review Environment Variables**: Ensure all are set
3. **Test Locally**: Run with production config
4. **OpenAI Dashboard**: Verify API key and usage

### **Security Incidents:**
- Monitor logs for attack patterns
- Rate limiting will auto-block bad actors
- All security events are logged

---

## 🔄 **Updates & Maintenance**

### **Regular Tasks:**
- **Weekly**: Review security logs
- **Monthly**: Update dependencies
- **Quarterly**: Rotate API keys
- **As Needed**: Scale based on usage

Your backend is now production-ready with enterprise-level security! 🚀🔒