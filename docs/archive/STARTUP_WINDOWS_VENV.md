> **历史归档，非当前开发依据。** 本文保留当时的设计或验收记录，可能包含已撤销规则。当前入口：[文档索引](../README.md)；业务规则：[WORKFLOW.md](../WORKFLOW.md)。归档日期：2026-09-27。

# Windows + VSCode + venv 启动指南

## 1. 后端

打开 VSCode Terminal：

```powershell
cd E:\your_project\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

如果 PowerShell 禁止运行脚本：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

然后重新激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

安装：

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

设置 SAM3：

```powershell
$env:SAM3_MODEL_ID="E:\path\to\facebook--sam3\snapshots\master"
$env:SAM3_DEVICE="cuda"
$env:SAM3_DTYPE="bfloat16"
```

启动：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 3000
```

## 2. 前端

打开第二个 Terminal：

```powershell
cd E:\your_project\frontend
npm install
npm run dev
```

打开 Vite 输出的地址。

## 3. Tracking 参数

默认：

```text
SAM3_TRACK_FRAMES=120
ANOMALY_REVIEW_LOOKBACK_FRAMES=5
```

例：

```powershell
$env:SAM3_TRACK_FRAMES="200"
$env:ANOMALY_REVIEW_LOOKBACK_FRAMES="5"
```

## 4. 后端测试

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests -v
```

## 5. 典型使用

```text
上传视频
  ↓
定位到需要开始追踪的当前帧
  ↓
人工 bbox
  ↓
点击 AI Tracking
  ↓
① 保存当前帧 JSON
② SAM3 持续 Tracking
  ↓
异常时定位 pausedFrame，否则定位 lastProcessedFrame
  ↓
人工确认/补框
  ↓
再次 AI Tracking
```


## 本次 UI 更新

- 已移除“AI 检测 / 分割”按钮，AI Tracking 保留。
- 新增“选择框”工具，位于“点标注”左侧，快捷键 `V`；点击已有 bbox 即可选中，选择框模式不创建/拖动 bbox。
- 单轮追踪帧数由 `SAM3_TRACK_FRAMES` 控制（包含 seed 帧）。
