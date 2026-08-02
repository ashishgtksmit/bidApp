-- PR15: soft-delete + global normalized registration uniqueness on cardetails.
--
-- Apply ONLY after preflight_normalized_reg_conflicts.py reports zero conflicts.
-- Do NOT rely on ORM create_all for production.

-- 1) Add columns (nullable first for backfill of normalizedCarRegNo)
ALTER TABLE cardetails
  ADD COLUMN normalizedCarRegNo VARCHAR(100) NULL,
  ADD COLUMN isDeleted TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN deletedAt TIMESTAMP NULL DEFAULT NULL,
  ADD COLUMN deletedBy VARCHAR(10) NULL DEFAULT NULL;

-- 2) Backfill normalizedCarRegNo from carRegNo (ASCII letters/digits only, upper)
-- MySQL: strip non-alnum via nested REPLACE is incomplete; prefer the Python apply script.
-- Placeholder UPDATE for simple cases (spaces/hyphens only):
UPDATE cardetails
SET normalizedCarRegNo = UPPER(REPLACE(REPLACE(TRIM(carRegNo), ' ', ''), '-', ''))
WHERE normalizedCarRegNo IS NULL OR normalizedCarRegNo = '';

-- 3) Enforce NOT NULL after successful backfill with no empty normalized values
ALTER TABLE cardetails
  MODIFY COLUMN normalizedCarRegNo VARCHAR(100) NOT NULL;

-- 4) Global unique index (active + soft-deleted)
CREATE UNIQUE INDEX uq_cardetails_normalizedCarRegNo
  ON cardetails (normalizedCarRegNo);
