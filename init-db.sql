-- UEEP Core Database Initialization

-- Create tables
CREATE TABLE IF NOT EXISTS system_info (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert initial data
INSERT INTO system_info (key, value) VALUES 
    ('version', '1.0.0'),
    ('deployment_date', CURRENT_TIMESTAMP::TEXT),
    ('environment', 'production')
ON CONFLICT (key) DO NOTHING;
