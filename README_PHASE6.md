# 阶段6 交付说明 — 接口后端 + Streamlit 交互前端

> StudyMate Agent · 多 Agent 智能学习助手
> 基于 **FastAPI + Streamlit + LangGraph + Ollama + Chroma**
> 支持两种运行模式：**前后端分离** 与 **一体化**

---

## 一、目录结构

```
StudyMate Agent/
├── api/                          # FastAPI 后端
│   ├── __init__.py
│   ├── config.py                 # 读取 .env，加载 Chroma 路径
│   ├── schemas.py                # Pydantic 请求/响应标准化模型
│   └── main.py                   # FastAPI 入口，4 个核心接口
├── frontend/                     # Streamlit 前端
│   ├── __init__.py
│   ├── app.py                    # 主页面 + 侧边栏 5 大导航
│   ├── api_client.py             # 前后端分离模式 HTTP 客户端
│   ├── workflow_bridge.py        # 一体化模式工作流桥接
│   ├── error_utils.py            # 统一错误弹窗工具
│   └── pages/                    # 5 大子页面
│       ├── __init__.py
│       ├── chat_page.py          # 智能问答
│       ├── upload_page.py        # 知识库上传
│       ├── exercise_page.py      # 习题解析
│       ├── literature_page.py    # 文献整理
│       └── system_check_page.py  # 系统检测
├── graph/                        # 阶段4 LangGraph 工作流
├── agents/                       # 阶段3 四大 Agent
├── tools/                        # 向量检索 + 代码沙箱
├── data_process/                 # 数据集处理 + 批量入库
├── start_server.bat              # 启动 FastAPI 后端（分离模式）
├── start_front.bat               # 启动 Streamlit 前端（分离模式）
├── start_integrated.bat          # 启动一体化模式（免后端）
├── env_check.py                  # 环境自检脚本
└── .env                          # 全局配置
```

---

## 二、运行模式说明

### 模式 A：前后端分离（推荐，适合多用户/生产环境）

```
┌─────────────┐   HTTP/JSON   ┌──────────────┐
│  Streamlit  │ ────────────> │   FastAPI    │
│  前端 :8501 │ <──────────── │   后端 :8000 │
└─────────────┘               └──────────────┘
                                   │
                                   ├──> Ollama :11434 (qwen3:4b)
                                   └──> Chroma  ./chroma_db/
```

- 前端只做展示，业务逻辑全部在后端
- 支持多前端同时连接
- 前端崩溃不影响后端会话

### 模式 B：一体化（适合单机学生使用，资源占用低）

```
┌─────────────────────────────────┐
│        Streamlit :8501          │
│  ┌───────────────────────────┐  │
│  │  直接调用 graph 工作流    │  │
│  │  直接调用 tools 沙箱      │  │
│  └───────────────────────────┘  │
│              │                   │
│              ├──> Ollama :11434  │
│              └──> Chroma         │
└─────────────────────────────────┘
```

- 无需启动 FastAPI
- Agent 在 Streamlit 进程内运行
- 启动更快、内存占用更低
- 适合本地单机使用

---

## 三、启动操作步骤

### 前置准备（首次运行必做）

1. **安装 uv**（如已安装可跳过）：
   ```powershell
   # Windows PowerShell
   irm https://astral.sh/uv/install.ps1 | iex
   ```

2. **安装依赖**：
   ```powershell
   uv sync
   ```

3. **启动 Ollama 并拉取模型**：
   ```powershell
   ollama serve                  # 启动服务
   ollama pull qwen3:4b          # 主模型
   ollama pull qwen3-embedding:4b  # 嵌入模型
   ```

4. **构建知识库**（首次或更新数据时）：
   ```powershell
   uv run python data_process/batch_build_kb.py --all
   ```

5. **环境自检**：
   ```powershell
   uv run python env_check.py
   ```

### 启动方式 1：前后端分离模式

**步骤 1**：双击 `start_server.bat` 启动后端
- 服务地址：http://localhost:8000
- 接口文档：http://localhost:8000/docs
- 健康检测：http://localhost:8000/api/health

**步骤 2**：双击 `start_front.bat` 启动前端
- 访问地址：http://localhost:8501
- 侧边栏会显示"后端在线"绿色提示

**步骤 3**：在浏览器中使用

### 启动方式 2：一体化模式

**只需一步**：双击 `start_integrated.bat`
- 访问地址：http://localhost:8501
- 无需启动后端，Streamlit 直接调用工作流
- 侧边栏会显示"一体化模式：直接调用 graph 工作流"

### 启动方式 3：命令行（适合调试）

```powershell
# 后端（分离模式）
uv run uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# 前端（分离模式）
$env:APP_MODE="separated"; uv run streamlit run frontend/app.py --server.port 8501

# 一体化模式
$env:APP_MODE="integrated"; uv run streamlit run frontend/app.py --server.port 8501
```

### 运行时切换模式

在 Streamlit 侧边栏可以直接切换：
- 选择"前后端分离（推荐）"或"一体化模式（免后端）"
- 切换后页面自动刷新，无需重启服务

---

## 四、端口修改方法

### 方法 1：修改 `.env`（推荐，永久生效）

编辑项目根目录的 `.env` 文件：

```ini
OLLAMA_HOST=http://localhost:11434
OLLAMA_LLM_MODEL=qwen3:4b
OLLAMA_EMBED_MODEL=qwen3-embedding:4b
CHROMA_PERSIST_DIRECTORY=./chroma_db/
FASTAPI_HOST=0.0.0.0
FASTAPI_PORT=8000          # 修改 FastAPI 端口
STREAMLIT_PORT=8501        # 修改 Streamlit 端口
```

修改后重启对应服务即可。

### 方法 2：启动命令临时指定

```powershell
# FastAPI 临时用 8010
uv run uvicorn api.main:app --host 0.0.0.0 --port 8010

# Streamlit 临时用 8502
uv run streamlit run frontend/app.py --server.port 8502
```

### 方法 3：bat 脚本自动读取

`start_server.bat` 与 `start_front.bat` 会自动从 `.env` 读取端口配置，无需手动改 bat。

### 前端连接后端地址

前端默认通过 `FASTAPI_HOST` + `FASTAPI_PORT` 拼接后端地址。
若后端运行在其他机器，可在 `.env` 增加：

```ini
API_BASE_URL=http://192.168.1.100:8000
```

---

## 五、Windows Chroma 数据库锁冲突解决方案

### 现象

- 接口报错：`Chroma database is locked by another process`
- 错误码：`CHROMA_LOCK`
- 前端弹窗："🔒 Chroma 数据库被锁定"

### 原因

Windows 文件系统对 SQLite 文件加锁，多个进程同时读写 Chroma 时会冲突。常见场景：
- 同时运行 `batch_build_kb.py` 与 Streamlit
- 同时启动多个 Streamlit 实例
- 前一次进程异常退出未释放锁

### 解决方案（按顺序尝试）

#### 方案 1：关闭其他写入进程

1. 任务管理器（Ctrl+Shift+Esc）→ 找到所有 `python.exe` 进程
2. 结束除了当前 Streamlit/FastAPI 之外的所有 Python 进程
3. 重启 Streamlit 与 FastAPI

#### 方案 2：删除 .lock 文件

```powershell
# PowerShell 执行
Remove-Item ./chroma_db/*.lock -Force -ErrorAction SilentlyContinue

# 递归删除所有子目录的 .lock
Get-ChildItem -Path ./chroma_db -Filter *.lock -Recurse | Remove-Item -Force
```

#### 方案 3：命令行强制结束占用进程

```powershell
# 查看占用 chroma_db 目录的进程
handle chroma_db        # 需要 Sysinternals handle.exe

# 结束所有 Python 进程（谨慎，会同时结束 Streamlit）
Get-Process python | Stop-Process -Force
```

#### 方案 4：备份后清空重建（终极方案）

```powershell
# 备份
Rename-Item ./chroma_db ./chroma_db_bak

# 重启服务后会自动创建空目录，再重新入库
uv run python data_process/batch_build_kb.py --all
```

### 预防措施

- ✅ 不要同时运行 `batch_build_kb.py` 和 Streamlit/FastAPI
- ✅ 使用一体化模式时只启动一个 Streamlit 实例
- ✅ 关闭服务时用 Ctrl+C 正常退出，避免强杀
- ✅ 系统检测页可实时查看 .lock 文件数量

---

## 六、4 个核心接口文档

### 1. POST /api/chat — 多 Agent 问答

**请求体**：
```json
{
  "query": "求解方程 x^2 - 5x + 6 = 0",
  "history": [
    {"role": "user", "content": "上一个是关于什么的？"}
  ],
  "temperature": 0.7
}
```

**响应**：
```json
{
  "success": true,
  "data": {
    "final_answer": "x=2 或 x=3...",
    "intent": "math_exercise",
    "retrieval_chunks": [
      {"content": "...", "score": 0.12, "metadata": {"source": "gsm8k"}}
    ],
    "execution_log": [
      {"node": "router", "status": "ok", "message": "路由完成", "timestamp": "..."}
    ],
    "reflection_output": "...",
    "elapsed": 3.45
  },
  "error": null,
  "elapsed": 3.45
}
```

### 2. POST /api/retrieve — 单独向量检索

**请求体**：
```json
{
  "query": "微积分基本定理",
  "k": 5,
  "filter": {"source": "lecture.pdf"},
  "score_threshold": null
}
```

### 3. POST /api/calc — 沙箱数理计算

**请求体**：
```json
{
  "code": "from sympy import symbols, solve\nx = symbols('x')\nsolve(x**2-5*x+6, x)",
  "timeout": 10
}
```

### 4. GET /api/health — 环境健康检测

**响应**：
```json
{
  "success": true,
  "data": {
    "status": "healthy",
    "ollama": {"host": "http://localhost:11434", "reachable": true, "models": [...]},
    "chroma": {"persist_directory": "...", "exists": true, "openable": true, "doc_count": 38081},
    "checks": [{"name": "Ollama 连通性", "passed": true, "message": "..."}]
  }
}
```

### 5. POST /api/kb/upload — 知识库文件上传入库（附加接口）

`multipart/form-data` 上传 PDF/docx 文件，自动解析+分块+入库。

---

## 七、错误码与友好弹窗

前端 `error_utils.py` 内置错误码 → 解决方案映射：

| 错误码 | 含义 | 弹窗标题 |
|--------|------|---------|
| `OLLAMA_OFFLINE` | Ollama 服务未启动 | 🚫 Ollama 服务未启动 |
| `CHROMA_LOCK` | Chroma 文件锁冲突 | 🔒 Chroma 数据库被锁定 |
| `CHROMA_FAILED` | Chroma 读写失败 | ⚠️ Chroma 向量库读写失败 |
| `SANDBOX_ERROR` | 沙箱执行失败 | 🧮 代码沙箱执行失败 |
| `TIMEOUT` | 请求超时 | ⏱️ 请求处理超时 |
| `BACKEND_OFFLINE` | 后端服务未启动 | 🔌 后端服务未启动 |
| `VALIDATION` | 参数校验失败 | 📝 参数校验失败 |
| `INTERNAL` | 服务内部错误 | 💥 服务内部错误 |

每个错误弹窗包含：
1. 主错误提示（红/黄色横幅）
2. 解决方案（可折叠）
3. 调试详情（默认折叠，含堆栈）

---

## 八、5 大页面功能速览

### 1. 💬 智能问答页
- 多轮对话历史缓存
- Agent 流转日志可折叠展示
- Chroma 召回原文片段可折叠展示
- LLM temperature 滑块调节（0~1.5）
- 一键导出对话为 Markdown

### 2. 📤 知识库上传页
- 批量上传 PDF/docx（拖拽或多选）
- 一键调用 `batch_build_kb` 入库 Chroma
- 入库后展示集合统计
- 检索预览验证入库效果

### 3. 🧮 习题解析页
- 左栏：习题问答（走 QAExerciseAgent 五步解题）
- 右栏：RestrictedPython+SymPy 沙箱验算
- 内置 4 个沙箱示例（求根/求导/积分/数值验算）
- temperature 滑块（习题推荐 0.3）

### 4. 📄 文献整理页
- Tab 1：文献辅助问答（走 RetrieveAgent 文献分支，输出创新点+GB/T 7714 参考文献）
- Tab 2：独立向量检索（按 source 过滤、score 阈值）
- 整理结果可导出 Markdown

### 5. 🩺 系统检测页
- 整体状态徽章（healthy/degraded/unhealthy）
- Ollama 服务状态 + 模型可用性
- Chroma 目录存在性 + 可打开性 + 锁文件检测
- 端口占用快速排查（8000/8501/11434）
- 常见问题修复指引（可折叠）

---

## 九、技术栈与版本

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | 3.12.x | 运行时 |
| uv | latest | 包管理 |
| FastAPI | 0.140.0 | 后端框架 |
| Uvicorn | 0.51.0 | ASGI 服务器 |
| Streamlit | 1.40.0 | 前端框架 |
| Pydantic | 2.13.4 | 数据校验 |
| LangGraph | 1.2.9 | 多 Agent 编排 |
| LangChain | 1.3.14 | LLM 抽象层 |
| langchain-ollama | 1.1.0 | Ollama 集成 |
| langchain-chroma | 0.2.3 | Chroma 集成 |
| ChromaDB | 0.5.15 | 向量数据库 |
| Ollama | qwen3:4b / qwen3-embedding:4b | LLM 服务 |
| RestrictedPython | 8.4 | 代码沙箱 |
| SymPy | 1.14.0 | 数理计算 |
| python-multipart | 0.0.32 | 文件上传 |

---

## 十、常见问题 FAQ

**Q1：启动后端报 `Address already in use`？**
A：端口被占用。执行 `netstat -ano | findstr :8000` 查看 PID，`taskkill /PID <PID> /F` 结束，或修改 `.env` 中 `FASTAPI_PORT`。

**Q2：前端显示"后端离线"？**
A：1) 确认 `start_server.bat` 已启动；2) 确认 `.env` 中 `FASTAPI_PORT` 与前端连接一致；3) 浏览器访问 http://localhost:8000 验证。

**Q3：智能问答一直转圈？**
A：1) 首次请求需加载 Chroma 542MB 索引，约 10-30 秒；2) 检查 Ollama 是否在线；3) 在系统检测页查看状态。

**Q4：上传文件后检索不到？**
A：1) 确认文件是 PDF/docx 格式；2) 入库耗时较长，等待 spinner 结束；3) 检查系统检测页 Chroma 文档数是否增加。

**Q5：沙箱报"invalid variable name"？**
A：RestrictedPython 限制变量名不能以下划线开头。把 `_x` 改为 `x` 即可。

**Q6：一体化模式和分离模式哪个好？**
A：
- 单机学生使用 → 一体化模式（启动快、资源省）
- 多用户/远程访问 → 前后端分离（前端可多实例）
- 调试接口 → 前后端分离（有 /docs 文档）

---

## 十一、交付清单

- ✅ `api/__init__.py` — 模块初始化
- ✅ `api/config.py` — 全局配置加载 + Chroma 路径工具
- ✅ `api/schemas.py` — Pydantic 请求/响应模型
- ✅ `api/main.py` — FastAPI 4 接口 + 全局异常 + 超时 + 统一错误格式
- ✅ `frontend/__init__.py` — 模块初始化
- ✅ `frontend/app.py` — 主页面 + 侧边栏 5 大导航
- ✅ `frontend/api_client.py` — 前后端分离模式 HTTP 客户端
- ✅ `frontend/workflow_bridge.py` — 一体化模式工作流桥接
- ✅ `frontend/error_utils.py` — 统一错误弹窗工具
- ✅ `frontend/pages/chat_page.py` — 智能问答页
- ✅ `frontend/pages/upload_page.py` — 知识库上传页
- ✅ `frontend/pages/exercise_page.py` — 习题解析页
- ✅ `frontend/pages/literature_page.py` — 文献整理页
- ✅ `frontend/pages/system_check_page.py` — 系统检测页
- ✅ `start_server.bat` — 后端启动脚本（更新）
- ✅ `start_front.bat` — 前端启动脚本（更新）
- ✅ `start_integrated.bat` — 一体化模式启动脚本（新增）
- ✅ `README_PHASE6.md` — 完整使用说明（本文档）

---

**完成时间**：2026-07-26
**交付人**：StudyMate Agent 开发组
