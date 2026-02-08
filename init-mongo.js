// This script runs when MongoDB starts for the first time
db = db.getSiblingDB('giveaway');

// Create collections with indexes
db.users.createIndex({ "user_id": 1 }, { unique: true });
db.accounts.createIndex({ "user_id": 1 });
db.accounts.createIndex({ "phone_number": 1 });
db.sessions.createIndex({ "account_id": 1 });
db.sessions.createIndex({ "created_at": 1 });
db.logs.createIndex({ "timestamp": 1 });
db.logs.createIndex({ "user_id": 1 });
db.logs.createIndex({ "event_type": 1 });

// Create initial settings
db.settings.insertOne({
  "monitoring_enabled": true,
  "owner_id": parseInt(process.env.OWNER_ID || "0"),
  "created_at": new Date()
});

print("Database initialized successfully");
