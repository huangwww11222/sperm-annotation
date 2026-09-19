export const clone = (v) => JSON.parse(JSON.stringify(v));
// JSON object key order is not meaningful; Python responses sort keys.
const canonical = (v) => Array.isArray(v) ? v.map(canonical) : v && typeof v === 'object'
    ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b)).map(([k, x]) => [k, canonical(x)])) : v;
export const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
export const normalized = (b) => b.map(v => Math.round(v * 1000) / 1000);
export function patches(a, b) {
    return b.filter((o, i) => !equal(normalized(o.bbox), normalized(a[i].bbox)))
        .map(o => ({ objectId: o.objectId, bbox: normalized(o.bbox) })).sort((x, y) => x.objectId - y.objectId);
}
export function metrics(a, b) {
    if (equal(a, b))
        return null;
    const intersection = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0])) * Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
    const union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection;
    const dx = (b[0] + b[2] - a[0] - a[2]) / 2, dy = (b[1] + b[3] - a[1] - a[3]) / 2;
    const dw = b[2] - b[0] - a[2] + a[0], dh = b[3] - b[1] - a[3] + a[1];
    const position = a[0] !== b[0] || a[1] !== b[1];
    return { iou: intersection / union, shift: Math.hypot(dx, dy), dw, dh,
        type: position && (dw || dh) ? '位置 + 尺寸调整' : position ? '位置调整' : '尺寸调整' };
}
export function dragBox(b, dx, dy, handle, w, h) {
    const min = 0.001;
    if (handle === 'move') {
        dx = Math.max(-b[0], Math.min(w - b[2], dx));
        dy = Math.max(-b[1], Math.min(h - b[3], dy));
        return normalized([b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]);
    }
    const next = [...b];
    if (handle.includes('l'))
        next[0] = Math.max(0, Math.min(b[2] - min, b[0] + dx));
    if (handle.includes('r'))
        next[2] = Math.min(w, Math.max(b[0] + min, b[2] + dx));
    if (handle.includes('t'))
        next[1] = Math.max(0, Math.min(b[3] - min, b[1] + dy));
    if (handle.includes('b'))
        next[3] = Math.min(h, Math.max(b[1] + min, b[3] + dy));
    return normalized(next);
}
export function crop(a, b, w, h) {
    const x = Math.max(0, Math.min(a[0], b[0]) - 25), y = Math.max(0, Math.min(a[1], b[1]) - 25);
    return `${x} ${y} ${Math.min(w, Math.max(a[2], b[2]) + 25) - x} ${Math.min(h, Math.max(a[3], b[3]) + 25) - y}`;
}
