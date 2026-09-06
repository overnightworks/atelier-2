/**
 * The canonical work-item order schema (`atelier2.contracts.work_items`'s
 * `WORK_ITEM_ORDER_SCHEMA_DOCUMENT`) ships no JSON file of its own, so every
 * e2e spec that publishes it through `POST /schema-revisions` carries its own
 * hand copy. This is the one such copy three specs now share, rather than
 * three literals that can drift apart -- it must hash to
 * `WORK_ITEM_ORDER_SCHEMA_REVISION` (`src/lib/orderSchema.ts`), which each
 * spec still asserts for itself after publishing it.
 */
export const WORK_ITEM_SCHEMA_DOCUMENT =
  '{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"body":{"type":"string"},"change_marker":{"maxLength":1024,"minLength":1,"type":"string"},"digest":{"pattern":"^[0-9a-f]{64}$","type":"string"},"kind":{"enum":["issue","change_request"],"type":"string"},"observed_at":{"pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$","type":"string"},"reference":{"maxLength":1024,"minLength":1,"type":"string"},"scope":{"items":{"type":"string"},"type":"array","uniqueItems":true}},"required":["body","change_marker","digest","kind","observed_at","reference","scope"],"title":"work item","type":"object"}';
