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

## 3. 帧差参数

默认：

```text
FRAME_DIFF_MAX_SEARCH_FRAMES=120
FRAME_DIFF_CONFIRM_FRAMES=3
FRAME_DIFF_MAX_CONFIRM_MISS=2
FRAME_DIFF_THRESHOLD=12
FRAME_DIFF_KNOWN_MARGIN=25
FRAME_DIFF_MIN_AREA=15
FRAME_DIFF_MIN_AREA_RATIO=0.55
FRAME_DIFF_TEMPLATE_THRESHOLD=0.35
```

例：

```powershell
$env:FRAME_DIFF_MAX_SEARCH_FRAMES="200"
$env:FRAME_DIFF_CONFIRM_FRAMES="3"
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
② 帧差分析
③ SAM3 Tracking
  ↓
定位到 newObjectFrame
  ↓
人工确认/补框
  ↓
再次 AI Tracking
```


## 本次 UI 更新

- 已移除“AI 检测 / 分割”按钮，AI Tracking 保留。
- 新增“选择框”工具，位于“点标注”左侧，快捷键 `V`；点击已有 bbox 即可选中，选择框模式不创建/拖动 bbox。
- 帧差规划默认开启后端逐帧日志，可由 `FRAME_DIFF_VERBOSE_LOG=0` 关闭。
