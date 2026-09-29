"""Build a small offline update for the deployed Chrome 93 compatibility issue.

Run npm run build --prefix frontend first. No models, credentials or data enter
the artifact. The installed backend must match the reviewed base or this patch.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
# Pre-patch app/main.py from commit 2488003 (also used by the offline delivery).
BASE_HASH = "b6f1d2a0c54b809ce731073dde6ef0049cff8c71599bdeb29b04a786a5c41a4c"


def build():
    dist = ROOT / "frontend/dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("先运行 npm run build --prefix frontend")
    main = (ROOT / "backend/app/main.py").read_bytes()
    files = {"backend/main.py": main}
    for path in sorted(dist.rglob("*")):
        if path.is_file():
            files["frontend/dist/" + path.relative_to(dist).as_posix()] = path.read_bytes()
    files["backend/Dockerfile"] = b"ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY main.py /app/backend/app/main.py\n"
    files["frontend/Dockerfile"] = b"ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY dist/ /usr/share/nginx/html/\n"
    files["manifest.json"] = json.dumps({"acceptedBackendHashes": [BASE_HASH, hashlib.sha256(main).hexdigest()]}, indent=2).encode()
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    output = ROOT / "output/hospital-compat-update.sh"
    output.parent.mkdir(exist_ok=True)
    installer = (ROOT / "scripts/install_compat_update.sh").read_bytes()
    output.write_bytes(installer + b"\n__COMPAT_PAYLOAD_BELOW__\n" + base64.encodebytes(buffer.getvalue()))
    output.chmod(0o755)
    print(f"{output}: {output.stat().st_size:,} bytes")
    print("SHA256: " + hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    build()
