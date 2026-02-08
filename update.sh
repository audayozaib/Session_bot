#!/bin/bash

# Update Bot Script
echo "🔄 Updating Telegram Bot..."

# Backup current database
echo "💾 Creating database backup..."
docker-compose exec mongodb mongodump --out /backup/$(date +%Y%m%d_%H%M%S)

# Pull latest changes
echo "📥 Pulling latest changes..."
git pull origin main

# Rebuild and restart
echo "🔨 Rebuilding and restarting..."
docker-compose down
docker-compose build --no-cache
docker-compose up -d

echo "✅ Update completed!"
