-- OpenBid Admin Console: admin identity + audit log.
-- Apply once to the `bidapp` database (MySQL 8). Idempotent.

CREATE TABLE IF NOT EXISTS admin_users (
  id                   INT AUTO_INCREMENT PRIMARY KEY,
  email                VARCHAR(200) NOT NULL,
  full_name            VARCHAR(200) NOT NULL,
  password_hash        VARCHAR(255) NOT NULL,
  role                 VARCHAR(20)  NOT NULL DEFAULT 'viewer',
  is_active            TINYINT(1)   NOT NULL DEFAULT 1,
  token_version        INT          NOT NULL DEFAULT 1,
  must_change_password TINYINT(1)   NOT NULL DEFAULT 1,
  failed_logins        INT          NOT NULL DEFAULT 0,
  locked_until         DATETIME     NULL,
  last_login_at        DATETIME     NULL,
  created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_admin_users_email (email),
  CONSTRAINT ck_admin_users_role CHECK (role IN ('super_admin','operations','finance','support','viewer'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_audit_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  admin_id    INT          NULL,
  admin_email VARCHAR(200) NOT NULL,
  action      VARCHAR(60)  NOT NULL,
  entity_type VARCHAR(40)  NOT NULL,
  entity_id   VARCHAR(64)  NULL,
  summary     VARCHAR(300) NULL,
  before_json TEXT         NULL,
  after_json  TEXT         NULL,
  ip          VARCHAR(64)  NULL,
  user_agent  VARCHAR(300) NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_admin_audit_entity (entity_type, entity_id),
  KEY ix_admin_audit_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The audit log is append-only. If the API connects with its own MySQL user,
-- revoke UPDATE/DELETE on it, e.g.:
--   REVOKE UPDATE, DELETE ON bidapp.admin_audit_log FROM 'openbid_api'@'%';
