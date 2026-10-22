import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  applyBestEffortAppearanceUpdates,
  buildVisitIdentity,
  customerAppearanceUpdates,
  normalizePlateDetection,
  optionalConfidence,
  parseVisitAppearance,
  parseVisitRouteAudit,
  resolveWriteLocation,
} from "../lambdas/insert_data/visit_schema.mjs";

const HASH = "a".repeat(64);

test("legacy recognized visits keep their stable vehicle hash", () => {
  const result = buildVisitIdentity({ licensePlate: HASH }, "request-1");
  assert.equal(result.ok, true);
  assert.equal(result.plateDetection, "recognized");
  assert.equal(result.vehicleHash, HASH);
  assert.equal(result.plateHash, HASH);
});

test("unidentified visits receive a retry-stable anonymous key", () => {
  const first = buildVisitIdentity(
    { plateDetection: "no_visible_plate" },
    "same-request",
  );
  const retry = buildVisitIdentity(
    { plateDetection: "no_visible_plate" },
    "same-request",
  );
  assert.equal(first.ok, true);
  assert.equal(first.visitId, retry.visitId);
  assert.equal(first.vehicleHash, retry.vehicleHash);
  assert.match(first.vehicleHash, /^UNIDENTIFIED#/);
  assert.equal(first.plateHash, null);
});

test("recognized visits require a real hash", () => {
  const result = buildVisitIdentity({ plateDetection: "recognized" }, "request-2");
  assert.equal(result.ok, false);
});

test("unrecognized visits cannot carry a customer identity hash", () => {
  const result = buildVisitIdentity(
    { licensePlate: HASH, plateDetection: "insufficient_confidence" },
    "request-3",
  );
  assert.equal(result.ok, false);
});

test("documented aliases normalize without widening the wire enum", () => {
  assert.equal(normalizePlateDetection("confirmed", true), "recognized");
  assert.equal(
    normalizePlateDetection("group_confidence_floor", false),
    "insufficient_confidence",
  );
  assert.equal(normalizePlateDetection("anything_else", false), null);
});

test("authenticated location wins over an untrusted body location", () => {
  assert.equal(resolveWriteLocation("ljs-1", "attacker-location"), "ljs-1");
  assert.equal(resolveWriteLocation(null, "attacker-location"), null);
});

test("appearance parser accepts Stream MMC and vehicle type aliases", () => {
  assert.deepEqual(
    parseVisitAppearance({
      color: " White ",
      make: "Toyota",
      model: "Camry",
      vehicleType: "Sedan",
    }),
    {
      color: "White",
      make: "Toyota",
      model: "Camry",
      vehicleType: "Sedan",
    },
  );
  assert.equal(parseVisitAppearance({ dominantColor: "green" }).color, "green");
  assert.equal(
    parseVisitAppearance({ vehicle_type: "Pickup Truck" }).vehicleType,
    "Pickup Truck",
  );
});

test("confidence parser accepts only finite unit-interval values", () => {
  assert.equal(optionalConfidence(0), 0);
  assert.equal(optionalConfidence("0.875"), 0.875);
  assert.equal(optionalConfidence("1.00"), 1);
  assert.equal(optionalConfidence(1), 1);
  assert.equal(optionalConfidence(-0.01), null);
  assert.equal(optionalConfidence(1.01), null);
  assert.equal(optionalConfidence("not-a-number"), null);
  assert.equal(optionalConfidence(true), null);
  assert.equal(optionalConfidence(false), null);
  assert.equal(optionalConfidence(" "), null);
  assert.equal(optionalConfidence(" 0.875"), null);
  assert.equal(optionalConfidence("0.875 "), null);
  assert.equal(optionalConfidence("1e0"), null);
  assert.equal(optionalConfidence([]), null);
  assert.equal(optionalConfidence([0.875]), null);
  assert.equal(optionalConfidence({ value: 0.875 }), null);
  assert.equal(optionalConfidence(null), null);
});

test("route audit parser preserves valid qualified trajectory evidence", () => {
  assert.deepEqual(
    parseVisitRouteAudit({
      triggeredAt: "2026-08-27T20:00:01.000Z",
      routeStatus: "QUALIFIED",
      routeReason: "entry_then_board_trajectory",
      routeTrackFingerprint: "b".repeat(64),
      trajectoryPoints: 17,
      dwellSeconds: 3.2,
    }),
    {
      triggeredAt: "2026-08-27T20:00:01.000Z",
      routeStatus: "qualified",
      routeReason: "entry_then_board_trajectory",
      routeTrackFingerprint: "b".repeat(64),
      trajectoryPoints: 17,
      dwellSeconds: 3.2,
    },
  );
});

test("malformed route numbers and identifiers are omitted, not coerced", () => {
  const parsed = parseVisitRouteAudit({
    triggeredAt: "not-a-date",
    routeStatus: "fabricated",
    routeTrackFingerprint: "not-a-fingerprint",
    trajectoryPoints: "17",
    dwellSeconds: -1,
  });
  assert.equal(parsed.triggeredAt, null);
  assert.equal(parsed.routeStatus, null);
  assert.equal(parsed.routeTrackFingerprint, null);
  assert.equal(parsed.trajectoryPoints, null);
  assert.equal(parsed.dwellSeconds, null);

  assert.equal(parseVisitRouteAudit({ trajectoryPoints: 2.5 }).trajectoryPoints, null);
  assert.equal(parseVisitRouteAudit({ dwellSeconds: Infinity }).dwellSeconds, null);
});

test("conditional appearance writes compare color and pair independently", () => {
  const updates = customerAppearanceUpdates(
    "white",
    "Honda",
    "Passport",
    {
      colorConfidence: 0.95,
      makeConfidence: 0.92,
      modelConfidence: 0.88,
    },
  );

  assert.equal(updates.length, 2);
  const color = updates.find((update) => update.kind === "color");
  const pair = updates.find((update) => update.kind === "make_model");
  assert.match(
    color.ConditionExpression,
    /:mmc_color_confidence > #mmc_color_confidence/,
  );
  assert.equal(color.ExpressionAttributeValues[":mmc_color_confidence"], 0.95);
  assert.match(
    pair.ConditionExpression,
    /:mmc_pair_confidence > #mmc_make_confidence/,
  );
  assert.match(
    pair.ConditionExpression,
    /:mmc_pair_confidence > #mmc_model_confidence/,
  );
  assert.equal(pair.ExpressionAttributeValues[":mmc_pair_confidence"], 0.88);
  assert.match(pair.UpdateExpression, /#mmc_make = :mmc_make/);
  assert.match(pair.UpdateExpression, /#mmc_model = :mmc_model/);
});

test("conditional appearance writes keep make/model and scores atomic", () => {
  assert.deepEqual(
    customerAppearanceUpdates(
      null,
      "Toyota",
      null,
      { makeConfidence: 0.99, modelConfidence: 0.99 },
    ),
    [],
  );

  const [partialScore] = customerAppearanceUpdates(
    null,
    "Toyota",
    "Camry",
    { makeConfidence: 0.99 },
  );
  assert.equal(partialScore.kind, "make_model");
  assert.equal(
    partialScore.ConditionExpression,
    "attribute_not_exists(#mmc_make) OR "
      + "attribute_type(#mmc_make, :mmc_null_type) OR "
      + "attribute_not_exists(#mmc_model) OR "
      + "attribute_type(#mmc_model, :mmc_null_type)",
  );
  assert.match(
    partialScore.UpdateExpression,
    /REMOVE #mmc_make_confidence, #mmc_model_confidence$/,
  );
  assert.equal(
    partialScore.ExpressionAttributeValues[":mmc_make_confidence"],
    undefined,
  );
  assert.equal(
    partialScore.ExpressionAttributeValues[":mmc_model_confidence"],
    undefined,
  );
});

test("unscored legacy appearance only fills absent customer fields", () => {
  const updates = customerAppearanceUpdates(
    "green",
    "Ford",
    "F-150",
  );
  const color = updates.find((update) => update.kind === "color");
  const pair = updates.find((update) => update.kind === "make_model");

  assert.equal(
    color.ConditionExpression,
    "attribute_not_exists(#mmc_color) OR "
      + "attribute_type(#mmc_color, :mmc_null_type)",
  );
  assert.match(
    color.UpdateExpression,
    /REMOVE #mmc_color_confidence$/,
  );
  assert.equal(
    color.ExpressionAttributeNames["#mmc_color_confidence"],
    "color_confidence",
  );
  assert.equal(
    pair.ConditionExpression,
    "attribute_not_exists(#mmc_make) OR "
      + "attribute_type(#mmc_make, :mmc_null_type) OR "
      + "attribute_not_exists(#mmc_model) OR "
      + "attribute_type(#mmc_model, :mmc_null_type)",
  );
});

test("appearance update failures are isolated per attribute group", async () => {
  const updates = [
    { kind: "color" },
    { kind: "make_model" },
  ];
  const attempted = [];
  const failures = [];

  await assert.doesNotReject(() => applyBestEffortAppearanceUpdates(
    updates,
    async (update) => {
      attempted.push(update.kind);
      if (update.kind === "color") throw new Error("temporary DynamoDB error");
    },
    (update, error) => failures.push([update.kind, error.message]),
  ));

  assert.deepEqual(attempted, ["color", "make_model"]);
  assert.deepEqual(failures, [["color", "temporary DynamoDB error"]]);
});

test("customer statistics persist before fail-open appearance updates", () => {
  const source = readFileSync(
    new URL("../lambdas/insert_data/index.mjs", import.meta.url),
    "utf8",
  );
  const statsWrite = source.indexOf(
    "await docClient.send(new UpdateCommand(customerUpdate));",
  );
  const appearanceWrite = source.indexOf(
    "await applyCustomerAppearanceUpdates(vehicleHash, appearanceUpdates);",
  );
  const visitWrite = source.indexOf(
    'new PutCommand({\n        TableName: "Visits"',
  );
  const helperStart = source.indexOf(
    "async function applyCustomerAppearanceUpdates",
  );
  const helperEnd = source.indexOf("\nfunction parseBody", helperStart);
  const appearanceHelper = source.slice(helperStart, helperEnd);

  assert.notEqual(statsWrite, -1);
  assert.notEqual(appearanceWrite, -1);
  assert.notEqual(visitWrite, -1);
  assert.notEqual(helperStart, -1);
  assert.notEqual(helperEnd, -1);
  assert.ok(statsWrite < appearanceWrite);
  assert.ok(appearanceWrite < visitWrite);
  assert.match(
    source,
    /ConditionExpression:\s*\n\s*"attribute_not_exists\(#customer_vehicle_hash\)"/,
  );
  assert.match(
    appearanceHelper,
    /console\.error\("CUSTOMER APPEARANCE UPDATE FAILED"/,
  );
  assert.match(
    appearanceHelper,
    /if \(!isConditionalCheckFailure\(error\)\)/,
  );
  assert.doesNotMatch(appearanceHelper, /throw error/);
  // Core customer creation remains fail-closed for non-conditional errors.
  assert.match(source, /if \(!isConditionalCheckFailure\(error\)\) throw error;/);
});
