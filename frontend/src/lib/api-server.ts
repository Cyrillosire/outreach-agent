import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { SESSION_COOKIE } from "@/lib/constants";
import { ApiError, unwrapEnvelope } from "@/lib/envelope";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

async function request(path: string): Promise<Response> {
  const session = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!session) redirect("/login");
  let response: Response;
  try {
    response = await fetch(`${BACKEND_URL}/api/v1${path}`, {
      headers: { cookie: `${SESSION_COOKIE}=${session}`, accept: "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(503, { code: "backend_unavailable", message: "The API server is not reachable." });
  }
  if (response.status === 401) redirect("/login");
  return response;
}

/**
 * Server Component fetch to the backend, forwarding only the session cookie.
 * A 401 redirects to /login; other failures throw ApiError for the nearest error boundary.
 */
export async function serverFetch<T>(path: string): Promise<T> {
  return (await unwrapEnvelope<T>(await request(path))).data;
}

/** Server fetch that keeps `meta` (pagination). */
export async function serverFetchPage<T, M>(path: string): Promise<{ data: T; meta: M }> {
  return unwrapEnvelope<T, M>(await request(path));
}

/** Server fetch returning null on 404 so pages can render Next's not-found UI. */
export async function serverFetchOrNull<T>(path: string): Promise<T | null> {
  const response = await request(path);
  if (response.status === 404) return null;
  return (await unwrapEnvelope<T>(response)).data;
}
