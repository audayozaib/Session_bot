#!/bin/bash

# Telegram Bot Deployment Script
echo "🚀 Starting Telegram Bot Deployment..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "❌ .env file not found. Please create it from .env.example"
    exit 1
fi

# Create necessary directories
mkdir -p logs ssl

# Stop existing containers
echo "🛑 Stopping existing containers..."
docker-compose down

# Pull latest images
echo "📥 Pulling latest images..."
docker-compose pull

# Build the bot image
echo "🔨 Building bot image..."
docker-compose build --no-cache

# Start services
echo "🔄 Starting services..."
docker-compose up -d

# Wait for services to be ready
echo "⏳ Waiting for services to be ready..."
sleep 30

# Check if services are running
echo "🔍 Checking service status..."
docker-compose ps

# Show logs
echo "📋 Showing recent logs..."
docker-compose logs --tail=50

# Health check
echo "🏥 Performing health check..."
if curl -f http://localhost/health > /dev/null 2>&1; then
    echo "✅ Deployment successful! Bot is running."
else
    echo "❌ Health check failed. Please check the logs."
    docker-compose logs bot
fi

echo "🎉 Deployment completed!"
echo "Use 'docker-compose logs -f bot' to follow logs"
echo "Use 'docker-compose down' to stop services"
