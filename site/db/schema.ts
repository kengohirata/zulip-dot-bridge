import { integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';
export const bridgeConfig = sqliteTable('bridge_config', {
  id: integer('id').primaryKey(),
  ownerId: text('owner_id').notNull(),
  endpoint: text('endpoint').notNull(),
  expectedRealm: text('expected_realm').notNull(),
  proxySecret: text('proxy_secret').notNull(),
  updatedAt: text('updated_at').notNull()
});
