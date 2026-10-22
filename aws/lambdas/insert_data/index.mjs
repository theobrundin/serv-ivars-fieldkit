import { timingSafeEqual } from "node:crypto";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  PutCommand,
  GetCommand,
  UpdateCommand,
  ScanCommand,
  DeleteCommand,
} from "@aws-sdk/lib-dynamodb";
import {
  ApiGatewayManagementApiClient,
  PostToConnectionCommand,
} from "@aws-sdk/client-apigatewaymanagementapi";
import {
  applyBestEffortAppearanceUpdates,
  buildVisitIdentity,
  customerAppearanceUpdates,
  optionalConfidence,
  optionalString,
  parseVisitAppearance,
  parseVisitRouteAudit,
  resolveWriteLocation,
} from "./visit_schema.mjs";

const docClient = DynamoDBDocumentClient.from(
  new DynamoDBClient({ region: "us-east-1" })
);
const DEVICES_TABLE = process.env.DEVICES_TABLE || "Devices";
const WEBSOCKET_URL =
  "https://74hg1j5ig3.execute-api.us-east-1.amazonaws.com/production";
const RETENTION_DAYS = 365;

const DAYPARTS = {
  breakfast: [6, 7, 8, 9, 10],
  lunch: [11, 12, 13, 14],
  dinner: [15, 16, 17, 18, 19, 20],
  late_night: [21, 22, 23, 0, 1, 2, 3, 4, 5],
};
function daypartFor(hour) {
  for (const [name, hours] of Object.entries(DAYPARTS)) {
    if (hours.includes(hour)) return name;
  }
  return "unknown";
}

const CORS_HEADERS = {
  "Content-Type": "application/json",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "Content-Type,x-device-id,x-api-key,Idempotency-Key",
  "Access-Control-Allow-Methods": "POST,OPTIONS",
};

function getHeader(event, name) {
  const headers = event.headers || {};
  const lower = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === lower) return value;
  }
  return null;
}

function getAuthorizerContext(event) {
  const auth =
    event.requestContext?.authorizer?.lambda ||
    event.requestContext?.authorizer ||
    {};
  return {
    deviceId: auth.deviceId || auth.device_id || null,
    locationId: auth.locationId || auth.location_id || null,
  };
}

function keysMatch(stored, provided) {
  if (!stored || !provided) return false;
  const a = Buffer.from(String(stored));
  const b = Buffer.from(String(provided));
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

async function authenticateDevice(event) {
  const fromAuthorizer = getAuthorizerContext(event);
  if (fromAuthorizer.deviceId) {
    if (!fromAuthorizer.locationId) {
      return {
        ok: false,
        status: 403,
        error: "authenticated device is missing location scope",
      };
    }
    return {
      ok: true,
      deviceId: fromAuthorizer.deviceId,
      locationId: fromAuthorizer.locationId,
    };
  }

  const deviceId = getHeader(event, "x-device-id");
  const apiKey = getHeader(event, "x-api-key");
  if (!deviceId || !apiKey) {
    return { ok: false, status: 401, error: "missing x-device-id or x-api-key" };
  }

  const result = await docClient.send(
    new GetCommand({ TableName: DEVICES_TABLE, Key: { device_id: deviceId } })
  );
  const device = result.Item;
  if (!device || device.status !== "active" || !keysMatch(device.api_key, apiKey)) {
    return { ok: false, status: 401, error: "unauthorized" };
  }
  return {
    ok: true,
    deviceId,
    locationId: device.location_id || null,
    device,
  };
}

function json(statusCode, body) {
  return { statusCode, headers: CORS_HEADERS, body: JSON.stringify(body) };
}

function isConditionalCheckFailure(error) {
  return error?.name === "ConditionalCheckFailedException"
    || error?.code === "ConditionalCheckFailedException"
    || error?.Code === "ConditionalCheckFailedException";
}

async function applyCustomerAppearanceUpdates(vehicleHash, updates) {
  await applyBestEffortAppearanceUpdates(
    updates,
    async (update) => {
      const { kind: _kind, ...expression } = update;
      await docClient.send(new UpdateCommand({
        TableName: "Customers",
        Key: { vehicle_hash: vehicleHash },
        ...expression,
      }));
    },
    (update, error) => {
      // A conditional miss means another request already stored equal or
      // stronger evidence. Any other failure is operationally relevant, but
      // appearance is descriptive only: log it without turning enrichment
      // into a failed request/retry. The next attribute group still gets a
      // chance.
      if (!isConditionalCheckFailure(error)) {
        console.error("CUSTOMER APPEARANCE UPDATE FAILED", {
          kind: update.kind,
          vehicle_hash_prefix: vehicleHash.slice(0, 12),
          error_name: error?.name || "Error",
          error_message: error?.message || String(error),
        });
      }
    },
  );
}

function parseBody(event) {
  if (!event.body) return {};
  return typeof event.body === "string" ? JSON.parse(event.body) : event.body;
}

function handleOptions(event) {
  const method =
    event.requestContext?.http?.method ||
    event.httpMethod ||
    event.requestContext?.httpMethod;
  if (method === "OPTIONS") return json(200, {});
  return null;
}

export const handler = async (event) => {
  const options = handleOptions(event);
  if (options) return options;

  try {
    const auth = await authenticateDevice(event);
    if (!auth.ok) return json(auth.status, { success: false, error: auth.error });

    const body = parseBody(event);
    const identity = buildVisitIdentity(
      body,
      getHeader(event, "idempotency-key")
    );
    if (!identity.ok) return json(400, { success: false, error: identity.error });

    const {
      visitId,
      plateDetection,
      recognized,
      plateHash,
      vehicleHash,
    } = identity;
    // Location is an authorization scope, never device-supplied data. A
    // compromised or misconfigured camera must not write into another site.
    const locationId = resolveWriteLocation(auth.locationId, body.locationId);
    if (!locationId) {
      return json(403, {
        success: false,
        error: "authenticated device is missing location scope",
      });
    }
    const item = body.item;
    const price = body.price;
    const receivedAt = new Date();
    const detectedAt = body.detectedAt || null;
    const visitTime = detectedAt ? new Date(detectedAt) : receivedAt;
    if (!Number.isFinite(visitTime.getTime())) {
      return json(400, { success: false, error: "detectedAt must be a valid timestamp" });
    }
    const timestamp = visitTime.toISOString();
    const hourUtc = new Date(timestamp).getUTCHours();
    const expiresAt =
      Math.floor(receivedAt.getTime() / 1000) + RETENTION_DAYS * 86400;

    const inferenceDurationMs = body.inferenceDurationMs ?? null;
    const detectionConfidence =
      body.detectionConfidence ?? body.topDetectionConfidence ?? null;
    const ocrConfidence = body.ocrConfidence ?? body.topOcrConfidence ?? null;
    const frameCount = body.frameCount ?? null;
    const detectionOutcome = body.detectionOutcome ?? null;
    const swVersion = body.swVersion ?? null;
    const encounterId = optionalString(body.encounterId, 128);
    const firstSeenAt = optionalString(body.firstSeenAt, 64);
    const lastSeenAt = optionalString(body.lastSeenAt, 64);
    const identifiedAt = optionalString(body.identifiedAt, 64);
    const diagnosticEpisodeId = optionalString(body.diagnosticEpisodeId, 128);
    // Reserved for menu-board handoff and physical-route audits. Malformed
    // optional telemetry is omitted without dropping the underlying Visit.
    const {
      triggeredAt,
      routeStatus,
      routeReason,
      routeTrackFingerprint,
      trajectoryPoints,
      dwellSeconds,
    } = parseVisitRouteAudit(body);

    let roundtripMs = null;
    let isClockSkewed = false;
    if (detectedAt) {
      roundtripMs = receivedAt.getTime() - new Date(detectedAt).getTime();
      if (roundtripMs < 0 || roundtripMs > 30_000) {
        isClockSkewed = true;
        roundtripMs = null;
      }
    }

    const existingVisit = await docClient.send(
      new GetCommand({
        TableName: "Visits",
        Key: { vehicle_hash: vehicleHash, timestamp },
      })
    );
    if (existingVisit.Item) {
      return json(200, {
        success: true,
        duplicate: true,
        visit_id: visitId,
        vehicle_id: plateHash,
        timestamp,
        location_id: locationId,
      });
    }

    let visitCount = 1;
    let isRepeat = false;
    let recommendation = null;
    let secondsSinceLastVisit = null;
    let prevLocationId = null;
    const thisDaypart = daypartFor(hourUtc);
    const appearance = parseVisitAppearance(body);
    const { color, make, model, vehicleType } = appearance;
    const colorConfidence = optionalConfidence(body.colorConfidence);
    const makeConfidence = optionalConfidence(body.makeConfidence);
    const modelConfidence = optionalConfidence(body.modelConfidence);
    const appearanceUpdates = customerAppearanceUpdates(
      color,
      make,
      model,
      { colorConfidence, makeConfidence, modelConfidence },
    );

    // Anonymous visits are deliberately excluded from Customers. Without a
    // stable plate hash, linking separate visits would manufacture identity.
    if (recognized) {
      const existing = await docClient.send(
        new GetCommand({
          TableName: "Customers",
          Key: { vehicle_hash: vehicleHash },
        })
      );

      let existingCustomer = existing.Item || null;
      if (!existingCustomer) {
        try {
          // Appearance is deliberately absent from this creation write. It is
          // applied below with the same confidence conditions used for an
          // existing customer, so concurrent first visits cannot let the last
          // Put overwrite a stronger profile.
          await docClient.send(
            new PutCommand({
              TableName: "Customers",
              Item: {
                vehicle_hash: vehicleHash,
                user_id: optionalString(body.userId, 128),
                visit_count: 1,
                first_seen: timestamp,
                last_seen: timestamp,
                location_id: locationId,
                avg_days_between_visits: null,
                avg_visits_per_month: null,
                common_daypart: thisDaypart,
                daypart_breakfast: thisDaypart === "breakfast" ? 1 : 0,
                daypart_lunch: thisDaypart === "lunch" ? 1 : 0,
                daypart_dinner: thisDaypart === "dinner" ? 1 : 0,
                daypart_late_night: thisDaypart === "late_night" ? 1 : 0,
                daypart_unknown: thisDaypart === "unknown" ? 1 : 0,
                ...(vehicleType ? { vehicle_type: vehicleType } : {}),
              },
              ConditionExpression:
                "attribute_not_exists(#customer_vehicle_hash)",
              ExpressionAttributeNames: {
                "#customer_vehicle_hash": "vehicle_hash",
              },
            })
          );
        } catch (error) {
          if (!isConditionalCheckFailure(error)) throw error;
          // A simultaneous first visit created the customer after our read.
          // Fetch that record consistently and process this request as the
          // repeat visit it now is; never discard its statistics.
          const raced = await docClient.send(
            new GetCommand({
              TableName: "Customers",
              Key: { vehicle_hash: vehicleHash },
              ConsistentRead: true,
            })
          );
          if (!raced.Item) throw error;
          existingCustomer = raced.Item;
        }
      }

      if (existingCustomer) {
        visitCount = existingCustomer.visit_count + 1;
        isRepeat = true;
        if (existingCustomer.last_seen) {
          secondsSinceLastVisit = Math.floor(
            (new Date(timestamp) - new Date(existingCustomer.last_seen)) / 1000
          );
        }
        prevLocationId = existingCustomer.location_id || null;
        let avgDaysBetween = existingCustomer.avg_days_between_visits ?? null;
        if (secondsSinceLastVisit !== null && secondsSinceLastVisit >= 0) {
          const gapDays = secondsSinceLastVisit / 86400;
          const priorGapCount = Math.max(0, visitCount - 2);
          avgDaysBetween =
            avgDaysBetween === null || priorGapCount === 0
              ? gapDays
              : (avgDaysBetween * priorGapCount + gapDays) / (priorGapCount + 1);
        }
        const firstSeen = new Date(existingCustomer.first_seen || timestamp);
        const spanDays = Math.max(1, (new Date(timestamp) - firstSeen) / 86400000);
        const avgVisitsPerMonth = visitCount / (spanDays / 30.44);
        const daypartCounts = {
          breakfast: existingCustomer.daypart_breakfast || 0,
          lunch: existingCustomer.daypart_lunch || 0,
          dinner: existingCustomer.daypart_dinner || 0,
          late_night: existingCustomer.daypart_late_night || 0,
          unknown: existingCustomer.daypart_unknown || 0,
        };
        daypartCounts[thisDaypart] = (daypartCounts[thisDaypart] || 0) + 1;
        const commonDaypart = Object.entries(daypartCounts).sort(
          (a, b) => b[1] - a[1]
        )[0][0];
        const customerUpdate = {
          TableName: "Customers",
          Key: { vehicle_hash: vehicleHash },
          UpdateExpression:
            "SET visit_count = :vc, last_seen = :ls, location_id = :loc, " +
            "avg_days_between_visits = :adb, avg_visits_per_month = :avm, " +
            "common_daypart = :cdp, daypart_breakfast = :d0, " +
            "daypart_lunch = :d1, daypart_dinner = :d2, " +
            "daypart_late_night = :d3, daypart_unknown = :d4" +
            (vehicleType ? ", vehicle_type = :vehicle_type" : ""),
          ExpressionAttributeValues: {
            ":vc": visitCount,
            ":ls": timestamp,
            ":loc": locationId,
            ":adb": avgDaysBetween,
            ":avm": Math.round(avgVisitsPerMonth * 100) / 100,
            ":cdp": commonDaypart,
            ":d0": daypartCounts.breakfast,
            ":d1": daypartCounts.lunch,
            ":d2": daypartCounts.dinner,
            ":d3": daypartCounts.late_night,
            ":d4": daypartCounts.unknown,
            ...(vehicleType ? { ":vehicle_type": vehicleType } : {}),
          },
        };
        await docClient.send(new UpdateCommand(customerUpdate));
      }

      // Run these after the unconditional statistics write. Equal or weaker
      // evidence is rejected atomically by DynamoDB without rejecting the
      // visit itself; color and make/model race independently.
      await applyCustomerAppearanceUpdates(vehicleHash, appearanceUpdates);
    }

    const telemetry = Object.fromEntries(
      Object.entries({
        device_id: auth.deviceId,
        detected_at: detectedAt,
        roundtrip_ms: !isClockSkewed && roundtripMs !== null ? roundtripMs : null,
        inference_duration_ms: inferenceDurationMs,
        detection_confidence: detectionConfidence,
        ocr_confidence: ocrConfidence,
        frame_count: frameCount,
        detection_outcome: detectionOutcome,
        sw_version: swVersion,
        is_clock_skewed: isClockSkewed || undefined,
      }).filter(([, value]) => value !== null && value !== undefined)
    );

    const visit = {
      vehicle_hash: vehicleHash,
      timestamp,
      visit_id: visitId,
      encounter_id: encounterId,
      vehicle_id: plateHash,
      user_id: optionalString(body.userId, 128),
      plate_hash: plateHash,
      vehicle_detected: true,
      plate_detection: plateDetection,
      color,
      make,
      model,
      vehicle_type: vehicleType,
      color_confidence: colorConfidence,
      make_confidence: makeConfidence,
      model_confidence: modelConfidence,
      location_id: locationId,
      item: item || null,
      price: price || null,
      visit_number: visitCount,
      is_repeat: isRepeat,
      seconds_since_last_visit: secondsSinceLastVisit,
      prev_location_id: prevLocationId,
      hour_utc: hourUtc,
      expires_at: expiresAt,
      received_at: receivedAt.toISOString(),
      first_seen_at: firstSeenAt,
      last_seen_at: lastSeenAt,
      identified_at: identifiedAt,
      triggered_at: triggeredAt,
      diagnostic_episode_id: diagnosticEpisodeId,
      route_status: routeStatus,
      route_reason: routeReason,
      route_track_fingerprint: routeTrackFingerprint,
      trajectory_points: trajectoryPoints,
      dwell_seconds: dwellSeconds,
      ...telemetry,
    };
    await docClient.send(
      new PutCommand({
        TableName: "Visits",
        Item: Object.fromEntries(
          Object.entries(visit).filter(([, value]) => value !== undefined)
        ),
      })
    );

    if (recognized) {
      if (visitCount === 2) recommendation = "REWARDS_INVITE";
      else if (visitCount >= 10) recommendation = "SUPER_REGULAR";
      else if (visitCount >= 5) recommendation = "VIP_CUSTOMER";
    }

    const payload = {
      visit_id: visitId,
      encounter_id: encounterId,
      triggered_at: triggeredAt,
      vehicle_id: plateHash,
      user_id: visit.user_id,
      vehicle_detected: true,
      plate_detection: plateDetection,
      color: visit.color,
      make: visit.make,
      model: visit.model,
      vehicle_type: visit.vehicle_type,
      visit_count: visitCount,
      visit_number: visitCount,
      is_repeat: isRepeat,
      recommendation,
      timestamp,
      location_id: locationId,
      status: !recognized
        ? "unidentified"
        : visitCount >= 10
          ? "super_regular"
          : visitCount >= 5
            ? "vip"
            : visitCount > 1
              ? "returning"
              : "new",
    };

    const connections = await docClient.send(
      new ScanCommand({
        TableName: "Connections",
        FilterExpression: "locationId = :loc",
        ExpressionAttributeValues: { ":loc": locationId },
      })
    );
    const wsClient = new ApiGatewayManagementApiClient({ endpoint: WEBSOCKET_URL });
    for (const conn of connections.Items || []) {
      try {
        await wsClient.send(
          new PostToConnectionCommand({
            ConnectionId: conn.connectionId,
            Data: JSON.stringify(payload),
          })
        );
      } catch {
        await docClient.send(
          new DeleteCommand({
            TableName: "Connections",
            Key: { connectionId: conn.connectionId },
          })
        );
      }
    }
    return json(200, { success: true, ...payload });
  } catch (error) {
    console.error("VISIT ERROR", error);
    return json(500, { success: false, error: error.message });
  }
};
