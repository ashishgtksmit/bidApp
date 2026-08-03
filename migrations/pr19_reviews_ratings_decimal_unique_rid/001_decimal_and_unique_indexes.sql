001_vendorreviews_category_decimal.sql
-- ALTER TABLE vendorreviews
--   MODIFY COLUMN driverBehaviour DECIMAL(2,1) NOT NULL,
--   MODIFY COLUMN punctuality DECIMAL(2,1) NOT NULL,
--   MODIFY COLUMN carCondition DECIMAL(2,1) NOT NULL,
--   MODIFY COLUMN cleanliness DECIMAL(2,1) NOT NULL;

002_customerreviews_general_rating_decimal.sql
-- ALTER TABLE customerreviews
--   MODIFY COLUMN generalRating DECIMAL(2,1) NOT NULL;

003_unique_indexes.sql
-- CREATE UNIQUE INDEX uq_vendorreviews_rid ON vendorreviews (RID);
-- CREATE UNIQUE INDEX uq_customerreviews_rid ON customerreviews (RID);

-- Apply via apply_migration.py after preflights succeed.
-- Do not run these ALTERs if duplicate RID or malformed rating preflight fails.
