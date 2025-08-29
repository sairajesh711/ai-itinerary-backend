#!/bin/bash

# Google Cloud Setup for AI Itinerary Backend
# Usage: ./setup-gcp.sh [PROJECT_ID]

set -e

PROJECT_ID=${1:-"ai-itinerary-$(date +%s)"}

echo "🚀 Setting up Google Cloud for AI Itinerary Backend..."
echo "Project ID: ${PROJECT_ID}"

# Step 1: Authentication and project setup
echo "🔐 Please ensure you're logged in:"
echo "Run these commands first if not done:"
echo "  gcloud auth login"
echo "  gcloud auth application-default login"
echo ""

# Create project (optional - skip if exists)
read -p "Create new project ${PROJECT_ID}? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    gcloud projects create ${PROJECT_ID}
    echo "✅ Project created"
fi

# Set project configuration
echo "⚙️  Configuring project settings..."
gcloud config set project ${PROJECT_ID}
gcloud config set run/region asia-south1
gcloud config set run/platform managed

# Enable required APIs
echo "🔌 Enabling required APIs..."
gcloud services enable run.googleapis.com
gcloud services enable containerregistry.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable cloudbuild.googleapis.com

echo "✅ Google Cloud setup complete!"
echo ""
echo "Next steps:"
echo "1. Run: ./create-secrets.sh"
echo "2. Run: ./deploy-mumbai.sh"
echo ""
echo "Project: ${PROJECT_ID}"
echo "Region: asia-south1 (Mumbai)"