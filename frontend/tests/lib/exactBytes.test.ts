import { describe, expect, it } from "vitest";

import { decodeUtf8Base64 } from "../../src/lib/exactBytes";
import { bytesBase64 } from "../support/exactBytes";

describe("decoding a base64 field as UTF-8 text", () => {
  it("round-trips every byte value a UTF-8 encoder can produce", () => {
    const text = "Grüße 東京";

    expect(decodeUtf8Base64(bytesBase64(new TextEncoder().encode(text)))).toBe(text);
  });

  it("names a field that is not readable UTF-8 as unreadable", () => {
    const invalidUtf8 = bytesBase64(Uint8Array.of(0xff, 0xfe));

    expect(decodeUtf8Base64(invalidUtf8)).toBeNull();
  });

  it("names a field that is not valid base64 as unreadable", () => {
    expect(decodeUtf8Base64("not base64!!")).toBeNull();
  });
});
