/**
 * Normalizes API endpoints to guarantee no duplicate slashes or malformed URLs
 * regardless of trailing slashes in NEXT_PUBLIC_API_URL or leading slashes in path.
 */
export function getApiEndpoint(path: string): string {
  const base = (process.env.NEXT_PUBLIC_API_URL || "").trim().replace(/\/+$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const cleanPath = normalizedPath.replace(/\/+/g, "/");
  return base ? `${base}${cleanPath}` : cleanPath;
}
