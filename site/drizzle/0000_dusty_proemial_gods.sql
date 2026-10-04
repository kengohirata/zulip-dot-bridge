CREATE TABLE `bridge_config` (
	`id` integer PRIMARY KEY NOT NULL,
	`owner_id` text NOT NULL,
	`endpoint` text NOT NULL,
	`expected_realm` text NOT NULL,
	`proxy_secret` text NOT NULL,
	`updated_at` text NOT NULL
);
