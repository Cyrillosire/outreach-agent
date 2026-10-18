import { ApiError } from "./envelope";

/** Human-readable message for a failed API call, including the first field error of a validation failure. */
export function errorMessage(err: unknown, fallback: string): string {
  if (!(err instanceof ApiError)) return fallback;
  if (err.code !== "validation_error" || !Array.isArray(err.details)) return err.message;
  const first = err.details[0] as { loc?: (string | number)[]; msg?: string } | undefined;
  if (!first) return err.message;
  const path = (first.loc ?? []).slice(1).join(" › ");
  return path ? `${path}: ${first.msg}` : (first.msg ?? err.message);
}
