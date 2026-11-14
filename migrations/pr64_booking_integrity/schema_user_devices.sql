-- PR64: install device bindings for related-party (self-deal) matching.
CREATE TABLE IF NOT EXISTS user_devices (
    id INT NOT NULL AUTO_INCREMENT,
    userAppId VARCHAR(64) NOT NULL,
    deviceId VARCHAR(128) NOT NULL,
    lastSeenAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    createdAt TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_user_devices_user_device (userAppId, deviceId),
    KEY ix_user_devices_deviceId (deviceId)
);
