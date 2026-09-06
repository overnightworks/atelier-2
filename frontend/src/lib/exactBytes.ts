/**
 * The readable text a base64 field carries, or null when the bytes are not the
 * UTF-8 this surface can show. Strict on purpose: a field that is not text the
 * operator can read is named as unreadable rather than rendered as mojibake.
 */
export function decodeUtf8Base64(base64: string): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(
      Uint8Array.from(atob(base64), (character) => character.codePointAt(0)!)
    );
  } catch {
    return null;
  }
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", copy.buffer);
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0")
  ).join("");
}
