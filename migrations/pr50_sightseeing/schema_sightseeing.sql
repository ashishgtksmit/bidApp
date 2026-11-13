-- PR50 sightseeing schema. Additive only; transfers never read these objects.
-- No foreign keys to legacy tables (requesttable, location_details) because their
-- engine/column types are not guaranteed; integrity is enforced in FastAPI.

CREATE TABLE IF NOT EXISTS ob_sightseeing_spots (
  spotId BIGINT NOT NULL AUTO_INCREMENT,
  locationId BIGINT NOT NULL,
  spotCode VARCHAR(64) NOT NULL,
  spotName VARCHAR(200) NOT NULL,
  permitRequired TINYINT(1) NOT NULL DEFAULT 0,
  typicalDurationMins INT NULL,
  isActive TINYINT(1) NOT NULL DEFAULT 1,
  sortOrder INT NOT NULL DEFAULT 0,
  createdAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updatedAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (spotId),
  UNIQUE KEY uq_ob_ss_spot_code (spotCode),
  KEY ix_ob_ss_spot_location (locationId, isActive, sortOrder)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS request_itinerary_days (
  dayId BIGINT NOT NULL AUTO_INCREMENT,
  requestId BIGINT NOT NULL,
  dayNo INT NOT NULL,
  travelDate DATE NOT NULL,
  startTime TIME NOT NULL,
  pickupLandmark VARCHAR(200) NOT NULL,
  createdAt DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (dayId),
  UNIQUE KEY uq_req_itin_day (requestId, dayNo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS request_itinerary_day_spots (
  id BIGINT NOT NULL AUTO_INCREMENT,
  dayId BIGINT NOT NULL,
  spotId BIGINT NOT NULL,
  sequenceNo INT NOT NULL,
  spotNameSnapshot VARCHAR(200) NOT NULL,
  permitRequiredSnapshot TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_req_itin_day_spot (dayId, spotId),
  KEY ix_req_itin_day_spot_seq (dayId, sequenceNo),
  CONSTRAINT fk_req_itin_day_spot_day FOREIGN KEY (dayId)
    REFERENCES request_itinerary_days (dayId) ON DELETE CASCADE,
  CONSTRAINT fk_req_itin_day_spot_spot FOREIGN KEY (spotId)
    REFERENCES ob_sightseeing_spots (spotId)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
