"""Container preflight: fail with actionable logs before serving requests."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile

from . import config

log = logging.getLogger("deployment")


def check_secret(secret: str) -> None:
    if len(secret) < 32 or secret in {"replace-with-at-least-32-random-bytes", "dev-secret-change-me"}:
        raise ValueError("JWT_SECRET 必须为至少 32 字符的独立随机密钥；运行 deploy.sh/deploy.ps1 初始化，或修改 .env。")


def check_model(directory: Path) -> None:
    if not (directory / "config.json").is_file():
        raise ValueError("SAM3_MODEL_HOST_PATH 必须指向含 config.json 的完整模型目录。")
    json.loads((directory / "config.json").read_text())
    if not any((directory / name).is_file() for name in ("preprocessor_config.json", "processor_config.json")):
        raise ValueError("SAM3 缺少处理器配置，请复制完整模型快照。")
    indexes = list(directory.glob("*.index.json"))
    weights = list(directory.glob("*.safetensors")) + list(directory.glob("pytorch_model*.bin"))
    for index in indexes:
        weight_map = json.loads(index.read_text()).get("weight_map", {})
        weights.extend(directory / name for name in set(weight_map.values()))
    if not weights or any(not p.is_file() or p.stat().st_size == 0 for p in weights):
        raise ValueError("SAM3 权重缺失、为空或分片不完整；检查模型目录和符号链接目标。")


def check() -> None:
    if config.APP_ENV == "production":
        check_secret(config.JWT_SECRET)
    for directory in {config.DATA_DIR, config.DB_FILE.parent, config.MEDIA_STORAGE_DIR, config.DATASET_EXPORT_DIR}:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"deployment-write-check")
    if config.SAM3_ENABLED:
        check_model(Path(config.MODEL_ID))
        import torch
        from transformers import Sam3TrackerVideoModel, Sam3TrackerVideoProcessor  # noqa: F401
        if config.DEVICE.startswith("cuda") and not torch.cuda.is_available():
            raise ValueError("CUDA 不可用；检查 NVIDIA 驱动、Container Toolkit 和 compose.gpu.yaml。")
        log.info("deployment.ready mode=tracking device=%s", config.DEVICE)
    else:
        log.info("deployment.ready mode=manual; AI Tracking 未启用，人工标注/审查/确认/导出可用。")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        check()
    except Exception:
        log.exception("deployment.preflight_failed 请修正部署配置后重新启动；业务数据未清理。")
        raise SystemExit(1)
