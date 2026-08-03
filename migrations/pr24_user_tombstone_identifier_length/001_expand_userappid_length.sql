-- PR24: Expand usertable.userAppId so soft-tombstone identifiers fit.
-- Example: {10-digit phone}.DELETED = 18 chars; .DELETED{n} grows further.
-- Target: VARCHAR(64). Idempotent apply script checks current column width.

ALTER TABLE usertable
  MODIFY COLUMN userAppId VARCHAR(64) NOT NULL;
