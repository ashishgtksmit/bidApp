-- PR59 per-day sightseeing bid prices. Additive only; transfer bids never read this table.
-- biddetails.bidAmount stays the total, so ranking, wallet and earnings are unchanged.
-- No foreign key to biddetails (legacy engine/column types); FastAPI deletes rows with the bid.

CREATE TABLE IF NOT EXISTS bid_day_prices (
  id BIGINT NOT NULL AUTO_INCREMENT,
  bidId BIGINT NOT NULL,
  dayNo INT NOT NULL,
  amount DECIMAL(11,2) NOT NULL,
  createdAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_bid_day_price (bidId, dayNo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
