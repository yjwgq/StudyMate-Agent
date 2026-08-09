# StudyMate Agent 云端模型接入指南

## 一、支持的服务商类型

StudyMate Agent 采用标准 OpenAI 兼容接口，支持以下主流云端大模型服务商：

| 服务商 | 推荐模型 | API 地址示例 |
|--------|----------|--------------|
| 阿里云通义千问 | qwen-plus, qwen-max | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| 百度文心一言 | ERNIE-4.0, ERNIE-3.5 | https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/completions |
| 智谱 AI | glm-4-plus, glm-4 | https://open.bigmodel.cn/api/paas/v4/chat/completions |
| DeepSeek | deepseek-chat | https://api.deepseek.com/v1 |
| 硅基流动 | Qwen2-7B-Instruct | https://api.siliconflow.cn/v1 |
| OpenAI | gpt-4o, gpt-4-turbo | https://api.openai.com/v1 |

**注意**：不同服务商的 API 接口格式可能略有差异，请根据服务商文档确认接口地址。

---

## 二、.env 配置填写规范

### 2.1 配置项说明

```ini
# 云端模型模式开关
USE_CLOUD_MODEL=true          # true=使用云端API, false=使用本地Ollama

# 云端API密钥
CLOUD_API_KEY=sk-xxxxxxxxxx   # 从服务商控制台获取

# 云端大模型配置
CLOUD_LLM_BASE_URL=https://api.example.com/v1     # LLM接口地址
CLOUD_LLM_MODEL_NAME=qwen-plus                    # LLM模型名称

# 云端向量嵌入模型配置
CLOUD_EMBED_BASE_URL=https://api.example.com/v1   # 嵌入接口地址（通常与LLM相同）
CLOUD_EMBED_MODEL_NAME=text-embedding-3-small     # 嵌入模型名称
```

### 2.2 配置示例

#### 阿里云通义千问

```ini
USE_CLOUD_MODEL=true
CLOUD_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
CLOUD_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
CLOUD_LLM_MODEL_NAME=qwen-plus
CLOUD_EMBED_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
CLOUD_EMBED_MODEL_NAME=text-embedding-v2
```

#### 智谱 AI

```ini
USE_CLOUD_MODEL=true
CLOUD_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxx
CLOUD_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
CLOUD_LLM_MODEL_NAME=glm-4-plus
CLOUD_EMBED_BASE_URL=https://open.bigmodel.cn/api/paas/v4
CLOUD_EMBED_MODEL_NAME=embedding-2
```

#### OpenAI

```ini
USE_CLOUD_MODEL=true
CLOUD_API_KEY=sk-proj-xxxxxxxxxxxxxxxx
CLOUD_LLM_BASE_URL=https://api.openai.com/v1
CLOUD_LLM_MODEL_NAME=gpt-4o
CLOUD_EMBED_BASE_URL=https://api.openai.com/v1
CLOUD_EMBED_MODEL_NAME=text-embedding-3-small
```

---

## 三、API Key 获取途径

### 阿里云通义千问
1. 访问 https://dashscope.console.aliyun.com/
2. 注册/登录阿里云账号
3. 创建 API Key（服务接入点选择"兼容模式"）
4. 充值或领取免费额度

### 智谱 AI
1. 访问 https://open.bigmodel.cn/
2. 注册/登录账号
3. 进入"密钥管理"创建 API Key
4. 领取免费额度或购买套餐

### 百度文心一言
1. 访问 https://console.bce.baidu.com/qianfan/
2. 注册/登录百度账号
3. 创建应用并获取 API Key / Secret Key
4. 注意：文心一言需要额外的签名处理

### OpenAI
1. 访问 https://platform.openai.com/
2. 注册/登录账号
3. 创建 API Key
4. 绑定支付方式

---

## 四、双模式切换操作指南

### 4.1 切换步骤

1. **编辑 .env 文件**：
   ```powershell
   notepad .env
   ```

2. **修改模式开关**：
   - 切换到云端模式：`USE_CLOUD_MODEL=true`
   - 切换到本地模式：`USE_CLOUD_MODEL=false`

3. **配置相应参数**：
   - 云端模式：确保 CLOUD_API_KEY、CLOUD_LLM_BASE_URL 等配置正确
   - 本地模式：确保 Ollama 服务已启动

4. **重启服务**：
   ```powershell
   # 重启后端
   start_server.bat
   
   # 重启前端
   start_front.bat
   ```

### 4.2 切换注意事项

- **切换模式后必须重启服务**，配置才会生效
- **本地模式需要启动 Ollama**，云端模式不需要
- **两种模式共享同一个 Chroma 向量库**，切换模式不会影响已入库的数据
- **建议在系统检测页验证**切换结果

---

## 五、云端常见报错排查

### 5.1 认证失败（CLOUD_AUTH_FAILED）

**现象**：API 调用返回 401/403 错误

**解决方案**：
1. 检查 `.env` 中 `CLOUD_API_KEY` 是否正确填写
2. 确认 API Key 没有过期或被撤销
3. 检查服务商是否要求额外的认证（如 IP 白名单）
4. 部分服务商需要使用特定的认证格式（如 Bearer Token）

### 5.2 网络连接失败（CLOUD_NETWORK_ERROR）

**现象**：无法连接到云端 API 服务器

**解决方案**：
1. 检查网络连接是否正常，尝试访问其他网站
2. 确认 `.env` 中 `CLOUD_LLM_BASE_URL` 地址是否正确
3. 检查防火墙或代理是否阻止了 API 请求
4. 尝试更换网络环境（如从公司网络切换到家庭网络）

### 5.3 请求超时（TIMEOUT）

**现象**：API 请求超过时间限制

**解决方案**：
1. 在 `.env` 中调大超时配置：`REQUEST_TIMEOUT=300`
2. 简化问题输入，减少单次请求的数据量
3. 检查网络延迟，尝试更换网络
4. 云端模式下可切换到本地模式

### 5.4 额度耗尽（CLOUD_QUOTA_EXCEEDED）

**现象**：API 返回额度不足或频率超限错误

**解决方案**：
1. 登录服务商控制台查看额度状态
2. 等待额度恢复（通常每日/每月刷新）
3. 升级套餐或购买更多额度
4. 切换到本地模式继续使用

### 5.5 模型不存在

**现象**：API 返回模型不存在或未授权使用

**解决方案**：
1. 确认 `.env` 中模型名称正确
2. 登录服务商控制台检查模型权限
3. 确认账号已开通该模型的使用权限

---

## 六、硬件适配建议

### 6.1 低配电脑（4GB-8GB 内存）

**推荐模式**：云端模式

**理由**：
- 本地 Ollama 运行 qwen3:4b 模型需要约 8GB+ 内存
- 云端模式仅占用少量网络带宽，不消耗本地计算资源
- 响应速度更快（云端服务器通常配置更高）

**配置建议**：
```ini
USE_CLOUD_MODEL=true
# 使用轻量级云端模型，减少延迟和费用
CLOUD_LLM_MODEL_NAME=qwen-plus    # 或其他 7B 参数的轻量模型
```

### 6.2 中高配电脑（16GB+ 内存）

**推荐模式**：本地模式

**理由**：
- 本地模型响应更稳定，不受网络影响
- 无 API 调用费用
- 数据完全本地化，隐私更安全

**配置建议**：
```ini
USE_CLOUD_MODEL=false
# 本地模型配置
OLLAMA_LLM_MODEL=qwen3:4b
OLLAMA_EMBED_MODEL=qwen3-embedding:4b
```

### 6.3 无网络环境

**强制模式**：本地模式

**理由**：
- 云端模式需要稳定的网络连接
- 无网络时自动切本地模式（需提前配置）

**配置建议**：
```ini
USE_CLOUD_MODEL=false
# 确保 Ollama 服务已启动，模型已拉取
OLLAMA_HOST=http://localhost:11434
```

### 6.4 混合场景

**策略**：日常使用云端模式，离线时切换本地模式

**操作步骤**：
1. 预先拉取本地模型：
   ```powershell
   ollama pull qwen3:4b
   ollama pull qwen3-embedding:4b
   ```
2. 有网络时使用云端模式（`USE_CLOUD_MODEL=true`）
3. 无网络时切换本地模式（`USE_CLOUD_MODEL=false`）

---

## 七、测试验证

### 7.1 运行云端测试脚本

```powershell
# 测试云端模式连通性
uv run python test_cloud_model.py --mode cloud

# 测试本地模式连通性
uv run python test_cloud_model.py --mode local

# 双模式对比测试
uv run python test_cloud_model.py --mode both
```

### 7.2 使用系统检测页

1. 启动 Streamlit 前端
2. 进入"系统检测"页面
3. 点击"立即检测"按钮
4. 查看检测结果，确认当前模式和配置状态

---

## 八、常见问题

### Q1：切换到云端模式后，检索功能是否受影响？

**A**：不受影响。Chroma 向量库仍使用本地文件持久化，仅向量生成层切换到云端嵌入模型。

### Q2：云端模式下，数据是否会上传到第三方服务器？

**A**：用户输入的问题和检索的文本片段会发送到云端模型进行处理。建议在隐私敏感场景使用本地模式。

### Q3：如何选择合适的云端模型？

**A**：
- 优先选择支持中文的模型（如 qwen、glm、ERNIE 系列）
- 习题解析推荐使用推理能力强的模型
- 普通问答可使用轻量级模型降低成本

### Q4：云端模式和本地模式可以同时运行吗？

**A**：不可以。同一时刻只能使用一种模式，通过 `.env` 配置切换。

---

## 九、技术支持

如遇到问题，请按以下顺序排查：

1. **查看日志**：`logs/agents.log` 包含详细的错误信息
2. **运行环境检测**：`uv run python env_check.py`
3. **运行云端测试**：`uv run python test_cloud_model.py --mode cloud`
4. **检查系统检测页**：启动前端后进入"系统检测"页面