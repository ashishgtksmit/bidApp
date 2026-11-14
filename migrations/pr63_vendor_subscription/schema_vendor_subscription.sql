-- PR63 vendor subscription tables. FastAPI create_all also creates these;
-- this file exists for databases where the API user lacks CREATE.

CREATE TABLE IF NOT EXISTS vendor_subscription_plans (
    id INT NOT NULL AUTO_INCREMENT,
    code VARCHAR(16) NOT NULL,
    name VARCHAR(60) NOT NULL,
    price_paise BIGINT NOT NULL,
    duration_days INT NOT NULL,
    active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_vendor_subscription_plans_code (code)
);

CREATE TABLE IF NOT EXISTS vendor_subscription_periods (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_uid BIGINT NOT NULL,
    plan_code VARCHAR(16) NOT NULL,
    amount_paise BIGINT NOT NULL DEFAULT 0,
    period_start DATETIME NOT NULL,
    period_end DATETIME NOT NULL,
    payment_method VARCHAR(12) NOT NULL,
    wallet_txn_ref VARCHAR(40) NULL,
    recharge_order_id VARCHAR(40) NULL,
    idempotency_key VARCHAR(100) NOT NULL,
    note VARCHAR(255) NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_vendor_subscription_periods_key (idempotency_key),
    KEY ix_vendor_subscription_periods_user_uid (user_uid)
);

CREATE TABLE IF NOT EXISTS vendor_subscription_state (
    id INT NOT NULL AUTO_INCREMENT,
    user_uid BIGINT NOT NULL,
    current_period_end DATETIME NULL,
    auto_renew TINYINT(1) NOT NULL DEFAULT 1,
    last_reminder_kind VARCHAR(20) NULL,
    last_reminder_period_end DATETIME NULL,
    last_reminder_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_vendor_subscription_state_user_uid (user_uid)
);
