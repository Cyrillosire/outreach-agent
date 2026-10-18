// Shared, environment-agnostic helpers for the backend's response envelopes.

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: unknown;
  request_id?: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: unknown;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id ?? null;
    this.details = body.details;
  }
}

function isErrorEnvelope(value: unknown): value is { error: ApiErrorBody } {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = (value as { error: unknown }).error;
  return typeof error === "object" && error !== null && "code" in error && "message" in error;
}

/** Parse a backend response into its `data` payload, or throw a typed ApiError. */
export async function unwrap<T>(response: Response): Promise<T> {
  return (await unwrapEnvelope<T>(response)).data;
}

/** Like `unwrap`, but also returns `meta` (pagination for list endpoints). */
export async function unwrapEnvelope<T, M = Record<string, unknown>>(
  response: Response,
): Promise<{ data: T; meta: M }> {
  if (response.status === 204) return { data: undefined as T, meta: {} as M };
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON bodies (proxy errors, HTML) are normalized below.
  }
  if (!response.ok) {
    if (isErrorEnvelope(body)) throw new ApiError(response.status, body.error);
    throw new ApiError(response.status, {
      code: response.status === 502 || response.status === 503 ? "backend_unavailable" : "http_error",
      message: `Request failed with status ${response.status}`,
    });
  }
  if (typeof body === "object" && body !== null && "data" in body) {
    const envelope = body as { data: T; meta?: M };
    return { data: envelope.data, meta: envelope.meta ?? ({} as M) };
  }
  throw new ApiError(response.status, { code: "malformed_response", message: "Unexpected response format" });
}

/** Read a cookie value from a `document.cookie`-style string. */
export function readCookie(cookieString: string, name: string): string | null {
  for (const part of cookieString.split(";")) {
    const [rawKey, ...rest] = part.trim().split("=");
    if (rawKey === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}
