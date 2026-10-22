import { createHash, randomUUID } from "node:crypto";

export const HEX_64 = /^[0-9a-f]{64}$/;
export const PLATE_DETECTIONS = new Set([
  "recognized",
  "insufficient_confidence",
  "no_visible_plate",
]);
export const ROUTE_STATUSES = new Set(["pending", "qualified", "rejected"]);

const PLATE_DETECTION_ALIASES = {
  true: "recognized",
  confirmed: "recognized",
  low_confidence: "insufficient_confidence",
  group_confidence_floor: "insufficient_confidence",
  no_plate_visible: "no_visible_plate",
  no_plate_detected: "no_visible_plate",
};

export function optionalString(value, maxLength = 120) {
  if (value === null || value === undefined) return null;
  const normalized = String(value).trim();
  if (!normalized) return null;
  return normalized.slice(0, maxLength);
}

export function optionalConfidence(value) {
  let parsed;
  if (typeof value === "number") {
    parsed = value;
  } else if (
    typeof value === "string"
    && /^(?:0(?:\.\d+)?|1(?:\.0+)?)$/.test(value)
  ) {
    parsed = Number(value);
  } else {
    return null;
  }
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) return null;
  return parsed;
}

export function parseVisitRouteAudit(body) {
  const rawStatus = optionalString(body.routeStatus, 32)?.toLowerCase() || null;
  const routeStatus = ROUTE_STATUSES.has(rawStatus) ? rawStatus : null;

  const rawTriggeredAt = optionalString(body.triggeredAt, 64);
  const triggeredAt = rawTriggeredAt
    && Number.isFinite(new Date(rawTriggeredAt).getTime())
    ? rawTriggeredAt
    : null;

  const rawFingerprint = optionalString(body.routeTrackFingerprint, 64)
    ?.toLowerCase() || null;
  const routeTrackFingerprint = rawFingerprint
    && HEX_64.test(rawFingerprint)
    ? rawFingerprint
    : null;

  const trajectoryPoints = typeof body.trajectoryPoints === "number"
    && Number.isInteger(body.trajectoryPoints)
    && body.trajectoryPoints >= 0
    ? body.trajectoryPoints
    : null;
  const dwellSeconds = typeof body.dwellSeconds === "number"
    && Number.isFinite(body.dwellSeconds)
    && body.dwellSeconds >= 0
    ? body.dwellSeconds
    : null;

  return {
    triggeredAt,
    routeStatus,
    routeReason: optionalString(body.routeReason, 80),
    routeTrackFingerprint,
    trajectoryPoints,
    dwellSeconds,
  };
}

export function customerAppearanceUpdates(
  color,
  make,
  model,
  confidences = {},
) {
  // Each returned update is deliberately independent. The caller first
  // persists core Customer statistics, then applies these with DynamoDB
  // conditions. A stale, weaker observation can therefore lose without
  // rejecting the request, and make/model always move as one pair.
  const updates = [];
  const colorConfidence = optionalConfidence(confidences.colorConfidence);
  const makeConfidence = optionalConfidence(confidences.makeConfidence);
  const modelConfidence = optionalConfidence(confidences.modelConfidence);

  if (color) {
    const scored = colorConfidence !== null;
    updates.push({
      kind: "color",
      UpdateExpression: scored
        ? "SET #mmc_color = :mmc_color, "
          + "#mmc_color_confidence = :mmc_color_confidence"
        : "SET #mmc_color = :mmc_color "
          + "REMOVE #mmc_color_confidence",
      ConditionExpression: scored
        ? "attribute_not_exists(#mmc_color) OR "
          + "attribute_type(#mmc_color, :mmc_null_type) OR "
          + "attribute_not_exists(#mmc_color_confidence) OR "
          + "attribute_type(#mmc_color_confidence, :mmc_null_type) OR "
          + ":mmc_color_confidence > #mmc_color_confidence"
        : "attribute_not_exists(#mmc_color) OR "
          + "attribute_type(#mmc_color, :mmc_null_type)",
      ExpressionAttributeNames: {
        "#mmc_color": "color",
        "#mmc_color_confidence": "color_confidence",
      },
      ExpressionAttributeValues: {
        ":mmc_color": color,
        ":mmc_null_type": "NULL",
        ...(scored
          ? { ":mmc_color_confidence": colorConfidence }
          : {}),
      },
    });
  }

  if (make && model) {
    // A partially scored pair is treated as unscored.  This avoids attaching
    // one classifier's confidence to an atomic make/model observation.
    const scored = makeConfidence !== null && modelConfidence !== null;
    const names = {
      "#mmc_make": "make",
      "#mmc_model": "model",
      "#mmc_make_confidence": "make_confidence",
      "#mmc_model_confidence": "model_confidence",
    };
    updates.push({
      kind: "make_model",
      UpdateExpression: scored
        ? "SET #mmc_make = :mmc_make, #mmc_model = :mmc_model, "
          + "#mmc_make_confidence = :mmc_make_confidence, "
          + "#mmc_model_confidence = :mmc_model_confidence"
        : "SET #mmc_make = :mmc_make, #mmc_model = :mmc_model "
          + "REMOVE #mmc_make_confidence, #mmc_model_confidence",
      ConditionExpression: scored
        ? "attribute_not_exists(#mmc_make) OR "
          + "attribute_type(#mmc_make, :mmc_null_type) OR "
          + "attribute_not_exists(#mmc_model) OR "
          + "attribute_type(#mmc_model, :mmc_null_type) OR "
          + "attribute_not_exists(#mmc_make_confidence) OR "
          + "attribute_type(#mmc_make_confidence, :mmc_null_type) OR "
          + "attribute_not_exists(#mmc_model_confidence) OR "
          + "attribute_type(#mmc_model_confidence, :mmc_null_type) OR "
          + ":mmc_pair_confidence > #mmc_make_confidence OR "
          + ":mmc_pair_confidence > #mmc_model_confidence"
        : "attribute_not_exists(#mmc_make) OR "
          + "attribute_type(#mmc_make, :mmc_null_type) OR "
          + "attribute_not_exists(#mmc_model) OR "
          + "attribute_type(#mmc_model, :mmc_null_type)",
      ExpressionAttributeNames: names,
      ExpressionAttributeValues: {
        ":mmc_make": make,
        ":mmc_model": model,
        ":mmc_null_type": "NULL",
        ...(scored ? {
          ":mmc_make_confidence": makeConfidence,
          ":mmc_model_confidence": modelConfidence,
          ":mmc_pair_confidence": Math.min(
            makeConfidence,
            modelConfidence,
          ),
        } : {}),
      },
    });
  }

  return updates;
}

export async function applyBestEffortAppearanceUpdates(
  updates,
  applyUpdate,
  onFailure = () => {},
) {
  // Appearance enriches a customer after its core statistics are written.
  // Treat each attribute group as an independent best-effort write so a
  // rejected color update cannot block make/model (or turn the request into
  // a retry before the Visit write).
  for (const update of updates) {
    try {
      await applyUpdate(update);
    } catch (error) {
      onFailure(update, error);
    }
  }
}

export function parseVisitAppearance(body) {
  return {
    color: optionalString(body.color ?? body.dominantColor, 80),
    make: optionalString(body.make, 80),
    model: optionalString(body.model, 80),
    vehicleType: optionalString(body.vehicleType ?? body.vehicle_type, 80),
  };
}

export function resolveWriteLocation(authorizerLocation, _requestBodyLocation) {
  // Location is an authorization scope. The second parameter makes the trust
  // boundary explicit and testable; request-body location is never trusted.
  return optionalString(authorizerLocation, 128);
}

export function normalizePlateDetection(value, hasPlateHash) {
  if (value === true) return "recognized";
  const raw = String(value ?? "").trim().toLowerCase();
  const normalized = PLATE_DETECTION_ALIASES[raw] || raw;
  if (!normalized) return hasPlateHash ? "recognized" : null;
  return PLATE_DETECTIONS.has(normalized) ? normalized : null;
}

function stableVisitId(idempotencyKey, suppliedVisitId, uuidFactory) {
  const supplied = optionalString(suppliedVisitId, 128);
  if (supplied) return supplied;
  const idempotency = optionalString(idempotencyKey, 256);
  if (idempotency) {
    return createHash("sha256").update(idempotency).digest("hex").slice(0, 32);
  }
  return uuidFactory();
}

export function buildVisitIdentity(
  body,
  idempotencyKey,
  uuidFactory = randomUUID,
) {
  const rawHash = optionalString(body.licensePlate, 64)?.toLowerCase() || null;
  if (rawHash && !HEX_64.test(rawHash)) {
    return { ok: false, error: "licensePlate must be a 64-char lowercase hex string" };
  }

  const plateDetection = normalizePlateDetection(body.plateDetection, Boolean(rawHash));
  if (!plateDetection) {
    return {
      ok: false,
      error:
        "plateDetection is required and must be recognized, " +
        "insufficient_confidence, or no_visible_plate",
    };
  }
  if (plateDetection === "recognized" && !rawHash) {
    return { ok: false, error: "recognized visits require licensePlate" };
  }
  if (plateDetection !== "recognized" && rawHash) {
    return { ok: false, error: "unrecognized visits must not include licensePlate" };
  }

  const visitId = stableVisitId(idempotencyKey, body.visitId, uuidFactory);
  return {
    ok: true,
    visitId,
    plateDetection,
    recognized: plateDetection === "recognized",
    plateHash: rawHash,
    vehicleHash: rawHash || `UNIDENTIFIED#${visitId}`,
  };
}
