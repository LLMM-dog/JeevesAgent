import { afterEach, describe, expect, it, vi } from "vitest";
import { startChat } from "./sse";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("startChat request body", () => {
  it("把图片 data URL 放进 POST body", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: null });
    vi.stubGlobal("fetch", fetchMock);

    startChat(
      {
        session_id: "s",
        content: "看这张图",
        images: ["data:image/png;base64,abc"],
      },
      { onEvent: () => {} },
    );

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const body = JSON.parse(String(init.body));
    expect(body.images).toEqual(["data:image/png;base64,abc"]);
  });
});
