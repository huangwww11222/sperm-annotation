// Blob cache has both a byte budget and an entry budget; failed requests are never cached.
export class FrameCache {
    maxEntries;
    maxBytes;
    entries = new Map();
    pending = new Map();
    bytes = 0;
    generation = 0;
    constructor(maxEntries = 12, maxBytes = 32 * 1024 * 1024) {
        this.maxEntries = maxEntries;
        this.maxBytes = maxBytes;
    }
    clear() { this.generation++; this.entries.clear(); this.pending.clear(); this.bytes = 0; }
    async get(key, load) {
        const cached = this.entries.get(key);
        if (cached) {
            this.entries.delete(key);
            this.entries.set(key, cached);
            return cached;
        }
        const ongoing = this.pending.get(key);
        if (ongoing)
            return ongoing;
        const generation = this.generation;
        const request = load().then(blob => {
            if (generation === this.generation && blob.size <= this.maxBytes) {
                this.entries.set(key, blob);
                this.bytes += blob.size;
                while (this.entries.size > this.maxEntries || this.bytes > this.maxBytes) {
                    const first = this.entries.keys().next().value;
                    this.bytes -= this.entries.get(first).size;
                    this.entries.delete(first);
                }
            }
            return blob;
        }).finally(() => { if (this.pending.get(key) === request)
            this.pending.delete(key); });
        this.pending.set(key, request);
        return request;
    }
}
