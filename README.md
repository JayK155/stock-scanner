# 📈 Stock Alert Scanner — Railway Deployment Guide

## What This Does
- Scans NASDAQ & NYSE every 60 seconds, 24/7 in the cloud
- Finds stocks breaking previous day high with price $1–$25, float <25M, volume >15K
- Shows results in a live web dashboard you can open from any device

---

## Step-by-Step Deployment on Railway

### Step 1 — Create a GitHub Account (if you don't have one)
1. Go to https://github.com and sign up (free)

### Step 2 — Upload the files to GitHub
1. Go to https://github.com/new to create a new repository
2. Name it: `stock-scanner`
3. Set it to **Private**
4. Click **Create repository**
5. Click **uploading an existing file**
6. Drag and drop ALL files from this folder:
   - app.py
   - requirements.txt
   - Procfile
   - railway.toml
7. Click **Commit changes**

### Step 3 — Create Railway Account
1. Go to https://railway.app
2. Click **Login with GitHub**
3. Authorize Railway

### Step 4 — Deploy on Railway
1. On Railway dashboard, click **New Project**
2. Click **Deploy from GitHub repo**
3. Select your `stock-scanner` repo
4. Railway will auto-detect it's a Python app and start building

### Step 5 — Add Your API Key as Environment Variable
1. In Railway, click on your project → **Variables** tab
2. Click **New Variable**
3. Add:
   - Name:  `POLYGON_API_KEY`
   - Value: `BjNzevikpEG7Yvnxbp0oVzS1MZ1K8TxA`
4. Click **Add**
5. Railway will automatically redeploy

### Step 6 — Get Your Public URL
1. In Railway, click **Settings** tab on your service
2. Under **Networking**, click **Generate Domain**
3. You'll get a URL like: `https://stock-scanner-production-xxxx.up.railway.app`
4. Open that URL in any browser — your dashboard is live!

---

## Pricing
- Railway gives you **$5 free credit/month**
- This app uses ~$1–2/month on the free tier
- To never worry about it, add a card and use the **Hobby plan ($5/mo)**

---

## How to View Your Dashboard
- Open your Railway URL from **any device** — phone, tablet, laptop
- The scanner runs in the cloud 24/7 even when your PC is off
- Click **Scan Now** to trigger an immediate scan
- Alerts auto-refresh every 15 seconds

---

## Troubleshooting
- **Build fails**: Make sure all 4 files are uploaded to GitHub
- **No alerts showing**: Markets may be closed (scanner runs 6AM–7PM ET)
- **Error in log bar**: Check your API key is set correctly in Railway Variables
- **App sleeping**: Make sure you're on Hobby plan, not free tier

---

## Files in This Package
| File | Purpose |
|------|---------|
| app.py | Main scanner + web dashboard server |
| requirements.txt | Python packages needed |
| Procfile | Tells Railway how to start the app |
| railway.toml | Railway configuration |
