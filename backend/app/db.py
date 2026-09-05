from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'member',
  password_hash TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS invitations (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'member',
  password_hash TEXT,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS otp_challenges (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  sent_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  scopes_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL UNIQUE,
  base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  config_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_groups (
  id TEXT PRIMARY KEY,
  connector_id TEXT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  name TEXT NOT NULL,
  selected INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(connector_id, external_id)
);
CREATE TABLE IF NOT EXISTS sync_cursors (
  source_group_id TEXT PRIMARY KEY REFERENCES source_groups(id) ON DELETE CASCADE,
  cursor_time TEXT,
  cursor_message_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracememo_message_cache (
  id TEXT PRIMARY KEY,
  connector_id TEXT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  source_group_id TEXT NOT NULL REFERENCES source_groups(id) ON DELETE CASCADE,
  external_message_id TEXT,
  content_hash TEXT NOT NULL,
  source_time TEXT,
  message_json TEXT NOT NULL,
  first_fetched_at TEXT NOT NULL,
  last_fetched_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(connector_id, source_group_id, content_hash)
);
CREATE TABLE IF NOT EXISTS tracememo_cache_state (
  connector_id TEXT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  source_group_id TEXT NOT NULL REFERENCES source_groups(id) ON DELETE CASCADE,
  first_fetched_at TEXT NOT NULL,
  last_fetched_at TEXT NOT NULL,
  last_start_at TEXT,
  last_end_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(connector_id, source_group_id)
);
CREATE TABLE IF NOT EXISTS ingest_runs (
  id TEXT PRIMARY KEY,
  source_group_id TEXT,
  kind TEXT NOT NULL,
  start_at TEXT,
  end_at TEXT,
  status TEXT NOT NULL,
  fetched_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS raw_messages (
  id TEXT PRIMARY KEY,
  connector_id TEXT,
  source_group_id TEXT,
  external_message_id TEXT,
  sender TEXT,
  sent_at TEXT,
  message_type TEXT NOT NULL,
  text_content TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL UNIQUE,
  is_recruitment INTEGER,
  recognition_status TEXT NOT NULL DEFAULT 'pending',
  recognized_at TEXT,
  recognition_error TEXT,
  retention_until TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(connector_id, source_group_id, external_message_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  raw_message_id TEXT REFERENCES raw_messages(id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  filename TEXT,
  mime_type TEXT,
  byte_size INTEGER NOT NULL,
  ocr_text TEXT,
  qr_values_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(raw_message_id, sha256)
);
CREATE TABLE IF NOT EXISTS processing_jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  raw_message_id TEXT REFERENCES raw_messages(id) ON DELETE CASCADE,
  company_id TEXT,
  status TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  next_attempt_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  processor TEXT,
  result_json TEXT,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_control (
  id INTEGER PRIMARY KEY CHECK(id=1),
  state TEXT NOT NULL DEFAULT 'paused',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processing_logs (
  id TEXT PRIMARY KEY,
  processing_job_id TEXT NOT NULL REFERENCES processing_jobs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  legal_name TEXT,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT,
  primary_industry TEXT NOT NULL DEFAULT 'other',
  secondary_industries_json TEXT NOT NULL DEFAULT '[]',
  website TEXT,
  company_nature TEXT,
  founded_at TEXT,
  company_size TEXT,
  headquarters TEXT,
  businesses_json TEXT NOT NULL DEFAULT '[]',
  highlights_json TEXT NOT NULL DEFAULT '[]',
  official_channels_json TEXT NOT NULL DEFAULT '[]',
  major_requirements_json TEXT NOT NULL DEFAULT '[]',
  company_tags_json TEXT NOT NULL DEFAULT '[]',
  summary_locked INTEGER NOT NULL DEFAULT 0,
  manual_overrides_json TEXT NOT NULL DEFAULT '{}',
  last_consolidated_at TEXT,
  public_researched_at TEXT,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_versions (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  profile_json TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  processor TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_relations (
  id TEXT PRIMARY KEY,
  parent_company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  child_company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(parent_company_id, child_company_id, relation_type)
);
CREATE TABLE IF NOT EXISTS company_claims (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  field_name TEXT NOT NULL,
  field_value TEXT NOT NULL,
  source_url TEXT,
  source_type TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.0,
  is_current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS company_public_findings (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  finding_type TEXT NOT NULL DEFAULT 'negative_news',
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_title TEXT,
  source_url TEXT NOT NULL,
  resolved_url TEXT,
  published_at TEXT,
  severity TEXT NOT NULL DEFAULT 'unknown',
  content_hash TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  UNIQUE(company_id, content_hash)
);
CREATE TABLE IF NOT EXISTS company_merge_rules (
  id TEXT PRIMARY KEY,
  left_company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  right_company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(left_company_id, right_company_id)
);
CREATE TABLE IF NOT EXISTS recruitment_batches (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  year INTEGER,
  season TEXT,
  recruitment_type TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recruitment_shared_details (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  batch_id TEXT REFERENCES recruitment_batches(id) ON DELETE SET NULL,
  evidence_id TEXT REFERENCES evidences(id) ON DELETE SET NULL,
  raw_message_id TEXT REFERENCES raw_messages(id) ON DELETE SET NULL,
  locations_json TEXT NOT NULL DEFAULT '[]',
  salary_json TEXT NOT NULL DEFAULT '{}',
  target_graduation_years_json TEXT NOT NULL DEFAULT '[]',
  education_requirements_json TEXT NOT NULL DEFAULT '[]',
  major_requirements_json TEXT NOT NULL DEFAULT '[]',
  application_url TEXT,
  deadline TEXT,
  process_json TEXT NOT NULL DEFAULT '[]',
  benefits_json TEXT NOT NULL DEFAULT '[]',
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  batch_id TEXT REFERENCES recruitment_batches(id),
  canonical_title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  department TEXT,
  locations_json TEXT NOT NULL DEFAULT '[]',
  recruitment_type TEXT NOT NULL DEFAULT 'unknown',
  employment_type TEXT NOT NULL DEFAULT 'unknown',
  headcount TEXT,
  education_json TEXT NOT NULL DEFAULT '[]',
  majors_json TEXT NOT NULL DEFAULT '[]',
  experience_requirement TEXT,
  salary_json TEXT NOT NULL DEFAULT '{}',
  responsibilities TEXT,
  requirements TEXT,
  benefits_json TEXT NOT NULL DEFAULT '[]',
  application_methods_json TEXT NOT NULL DEFAULT '[]',
  contacts_json TEXT NOT NULL DEFAULT '[]',
  explicit_deadline TEXT,
  effective_posted_at TEXT,
  last_effective_posted_at TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  industry_codes_json TEXT NOT NULL DEFAULT '[]',
  job_function_codes_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_versions (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  raw_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 0,
  UNIQUE(job_id, content_hash)
);
CREATE TABLE IF NOT EXISTS evidences (
  id TEXT PRIMARY KEY,
  company_id TEXT REFERENCES companies(id) ON DELETE CASCADE,
  job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
  raw_message_id TEXT REFERENCES raw_messages(id) ON DELETE SET NULL,
  artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  source_url TEXT,
  source_type TEXT NOT NULL,
  excerpt TEXT,
  location TEXT,
  observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recruitment_events (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  batch_id TEXT REFERENCES recruitment_batches(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  event_type TEXT NOT NULL,
  start_at TEXT,
  end_at TEXT,
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  format TEXT NOT NULL DEFAULT 'unknown',
  city TEXT,
  campus TEXT,
  location TEXT,
  application_url TEXT,
  audience TEXT,
  notes TEXT,
  job_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'upcoming',
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recruitment_event_versions (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES recruitment_events(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recruitment_event_evidences (
  event_id TEXT NOT NULL REFERENCES recruitment_events(id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidences(id) ON DELETE CASCADE,
  PRIMARY KEY(event_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_job_states (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'interested',
  favorite INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user_id, job_id)
);
CREATE TABLE IF NOT EXISTS application_events (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_notes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_tags (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS job_tag_links (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  tag_id TEXT NOT NULL REFERENCES user_tags(id) ON DELETE CASCADE,
  PRIMARY KEY(user_id, job_id, tag_id)
);
CREATE TABLE IF NOT EXISTS user_follows (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, company_id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  read_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY,
  provider_name TEXT,
  model_name TEXT,
  task_type TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS backups (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  snapshot_name TEXT,
  remote_path TEXT,
  manifest_json TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
  entity_type UNINDEXED,
  entity_id UNINDEXED,
  title,
  body,
  tokenize='unicode61'
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_group_time ON raw_messages(source_group_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_tracememo_cache_group_time ON tracememo_message_cache(connector_id, source_group_id, source_time);
CREATE INDEX IF NOT EXISTS idx_tracememo_cache_external_id ON tracememo_message_cache(connector_id, source_group_id, external_message_id);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_status ON processing_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_processing_logs_job ON processing_logs(processing_job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_shared_details_company ON recruitment_shared_details(company_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_claims_company ON company_claims(company_id);
CREATE INDEX IF NOT EXISTS idx_public_findings_company ON company_public_findings(company_id, retrieved_at);
CREATE INDEX IF NOT EXISTS idx_recruitment_events_time ON recruitment_events(start_at, company_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or config.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def init_db() -> None:
    config.ensure_dirs()
    with connect() as connection:
        connection.executescript(SCHEMA)
        migrations = {
            "users": {
                "password_hash": "TEXT",
            },
            "invitations": {
                "password_hash": "TEXT",
            },
            "processing_jobs": {
                "company_id": "TEXT",
                "stage": "TEXT NOT NULL DEFAULT 'queued'",
                "next_attempt_at": "TEXT",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "processor": "TEXT",
                "result_json": "TEXT",
                "started_at": "TEXT",
                "finished_at": "TEXT",
            },
            "raw_messages": {
                "recognition_status": "TEXT NOT NULL DEFAULT 'pending'",
                "recognized_at": "TEXT",
                "recognition_error": "TEXT",
            },
            "companies": {
                "company_nature": "TEXT",
                "founded_at": "TEXT",
                "company_size": "TEXT",
                "headquarters": "TEXT",
                "businesses_json": "TEXT NOT NULL DEFAULT '[]'",
                "highlights_json": "TEXT NOT NULL DEFAULT '[]'",
                "official_channels_json": "TEXT NOT NULL DEFAULT '[]'",
                "major_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
                "company_tags_json": "TEXT NOT NULL DEFAULT '[]'",
                "summary_locked": "INTEGER NOT NULL DEFAULT 0",
                "manual_overrides_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_consolidated_at": "TEXT",
                "public_researched_at": "TEXT",
            },
            "recruitment_shared_details": {
                "target_graduation_years_json": "TEXT NOT NULL DEFAULT '[]'",
                "education_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
                "major_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
                "application_url": "TEXT",
                "deadline": "TEXT",
                "process_json": "TEXT NOT NULL DEFAULT '[]'",
                "benefits_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        }
        for table, columns in migrations.items():
            existing_columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_processing_jobs_ready ON processing_jobs(status, next_attempt_at, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_messages_recognition_status ON raw_messages(recognition_status, created_at)"
        )
        connection.execute(
            """UPDATE raw_messages
               SET recognition_status='succeeded',recognized_at=COALESCE(recognized_at,created_at),recognition_error=NULL
               WHERE recognition_status='pending' AND is_recruitment IS NOT NULL"""
        )
        connection.execute(
            """UPDATE raw_messages
               SET recognition_status=CASE
                   WHEN p.status IN ('needs_review','paused_quota','failed') THEN 'needs_review'
                   WHEN p.status='canceled' THEN 'canceled'
                   WHEN p.status='running' THEN 'running'
                   ELSE 'pending'
               END,
                   recognition_error=CASE WHEN p.status IN ('needs_review','paused_quota','failed') THEN p.error ELSE NULL END
               FROM processing_jobs p
               WHERE p.raw_message_id=raw_messages.id
                 AND raw_messages.recognition_status='pending'
                 AND raw_messages.is_recruitment IS NULL"""
        )
        connection.execute(
            "INSERT OR IGNORE INTO queue_control(id,state,updated_at) VALUES(1,'paused',?)",
            (utc_now(),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '4')"
        )
        connection.execute(
            """INSERT OR IGNORE INTO system_settings(key, value_json, updated_at)
               SELECT 'import_days', value_json, updated_at
               FROM system_settings
               WHERE key='initial_import_days'"""
        )
        defaults = {
            "sync_interval_minutes": 10,
            "initial_import_days": 30,
            "import_days": 30,
            "redaction_enabled": False,
            "local_ocr_fallback_enabled": False,
            "llm_input_budget": 1_000_000,
            "llm_output_budget": 200_000,
            "llm_budget_warning_percent": 80,
            "ordinary_retention_days": 30,
            "possibly_expired_days": 45,
            "tracememo_base_url": "http://127.0.0.1:6131/api/v1",
            "llm_provider": {"enabled": False, "api_style": "chat_completions"},
            "smtp": {"enabled": False},
            "search": {"enabled": True, "cache_days": 30},
            "backup": {"enabled": False, "schedule": "02:00", "retention_count": 30},
            "agent_api_enabled": False,
            "otp_login_enabled": False,
            "processing_engine": "codex",
            "model_concurrency": 2,
            "codex_concurrency": 1,
            "extract_concurrency": 4,
            "codex_model": "gpt-5.6-luna",
            "processing_log_retention_days": 30,
        }
        for key, value in defaults.items():
            connection.execute(
                "INSERT OR IGNORE INTO system_settings(key, value_json, updated_at) VALUES(?, ?, ?)",
                (key, __import__("json").dumps(value, ensure_ascii=False), utc_now()),
            )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(sql, params).fetchone()


def all_rows(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(sql, params).fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with connect() as connection:
        cursor = connection.execute(sql, params)
        return cursor.rowcount
