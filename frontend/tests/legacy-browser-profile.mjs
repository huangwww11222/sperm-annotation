// Disable the APIs absent in Chrome 93 / HTTP. This does not claim to emulate
// the whole Chrome 93 rendering engine; run on the hospital client for final acceptance.
export async function useLegacyBrowserAPIs(page) {
  await page.addInitScript(() => {
    Object.defineProperty(AbortSignal, 'timeout', { value: undefined, configurable: true })
    Object.defineProperty(Crypto.prototype, 'randomUUID', { value: undefined, configurable: true })
    Object.defineProperty(HTMLVideoElement.prototype, 'requestVideoFrameCallback', { value: undefined, configurable: true })
  })
}
