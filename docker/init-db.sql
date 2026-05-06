-- Environmental Platform — Database Initialization
-- Auto-executed by PostgreSQL on first container start

-- ═══ Extensions ═══
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- Trigram for fuzzy text search

-- ═══ Users ═══
CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(36) PRIMARY KEY DEFAULT uuid_generate_v4()::text,
    email           VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    phone           VARCHAR(20),
    enterprise_name VARCHAR(255),
    is_verified     BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_enterprise ON users(enterprise_name);

-- ═══ Reports (crawled documents) ═══
CREATE TABLE IF NOT EXISTS reports (
    id              SERIAL PRIMARY KEY,
    url_hash        VARCHAR(64) UNIQUE NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    source          VARCHAR(64),          -- 'mee', 'cninfo'
    section         VARCHAR(64),
    file_type       VARCHAR(16),          -- 'pdf', 'docx', 'xlsx', 'txt', 'html'
    file_url        TEXT,
    published_at    TIMESTAMPTZ,
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_meta        JSONB
);
CREATE INDEX IF NOT EXISTS idx_reports_url_hash ON reports(url_hash);
CREATE INDEX IF NOT EXISTS idx_reports_source ON reports(source);
CREATE INDEX IF NOT EXISTS idx_reports_published_at ON reports(published_at);
CREATE INDEX IF NOT EXISTS idx_reports_scraped_at ON reports(scraped_at);
CREATE INDEX IF NOT EXISTS idx_reports_file_type ON reports(file_type);
-- GIN index for JSONB metadata queries
CREATE INDEX IF NOT EXISTS idx_reports_raw_meta ON reports USING GIN(raw_meta);

-- ═══ Document Embeddings ═══
CREATE TABLE IF NOT EXISTS document_embeddings (
    id              SERIAL PRIMARY KEY,
    report_id       INTEGER REFERENCES reports(id) ON DELETE CASCADE,
    embedding       vector(768),          -- For sentence-transformers (384/768 dims)
    model_name      VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_embeddings_report ON document_embeddings(report_id);

-- ═══ Audit Log ═══
CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    user_id         VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
    action          VARCHAR(64) NOT NULL, -- 'login', 'search', 'upload', 'download'
    resource        VARCHAR(255),         -- e.g., report URL, document hash
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

-- ═══ Sample Data (dev only) ═══
-- INSERT INTO users (id, email, hashed_password, enterprise_name, is_verified)
-- VALUES ('demo-user', 'demo@example.com', '$2b$12$...', 'Demo Corp', TRUE);
