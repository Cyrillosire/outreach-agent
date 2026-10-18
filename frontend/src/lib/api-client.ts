"use client";

import { CSRF_COOKIE, CSRF_HEADER } from "@/lib/constants";
import { readCookie, unwrap } from "@/lib/envelope";

/** Browser-side API call through the same-origin /api proxy, with CSRF protection on mutations. */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  // JSON bodies are strings; FormData (file uploads) must keep the browser-generated multipart boundary.
  if (typeof init.body === "string" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (method !== "GET" && method !== "HEAD") {
    const csrf = readCookie(document.cookie, CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }
  const response = await fetch(`/api/v1${path}`, { ...init, method, headers, credentials: "same-origin" });
  return unwrap<T>(response);
}
