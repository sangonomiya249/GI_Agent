# 🧠 LLM 多提供商配置指南

> **更新说明**: 项目已支持多个大模型API提供商，通过 `.env` 配置文件灵活切换，无需修改代码。

## 📝 配置方式

### 1️⃣ **GitHub 模型** (默认)
```ini
LLM_PROVIDER=github
GITHUB_TOKEN=your_github_token_here
MODEL_NAME=gpt-4o-mini
```

### 2️⃣ **OpenAI API**
```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxx...
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o
```

### 3️⃣ **nVidia API**
```ini
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=nvapi-xxx...
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
MODEL_NAME=meta/llama-3.1-405b-instruct
```

### 4️⃣ **自定义API** (如交大API、其他兼容OpenAI的服务)
```ini
LLM_PROVIDER=custom
CUSTOM_API_KEY=your_api_key_here
CUSTOM_BASE_URL=https://your-api-url.com/v1
MODEL_NAME=your-model-name
```

### 5️⃣ **本地模型** (Ollama、vLLM 等，无需 API Key)
```ini
LLM_PROVIDER=local
LOCAL_BASE_URL=http://localhost:8000/v1
MODEL_NAME=llama2
```

## 🚀 使用步骤

1. **打开 `.env` 文件**

2. **选择要使用的提供商**，修改 `LLM_PROVIDER` 为：
   - `github` (GitHub Models - 推荐，免费)
   - `openai` (OpenAI API)
   - `nvidia` (nVidia API)
   - `custom` (自定义API/交大API)
   - `local` (本地模型 - Ollama、vLLM 等)

3. **配置对应的 API Key 和 URL**

4. **运行程序**
   ```bash
   # CLI 模式
   python main.py
   
   # 飞书机器人模式
   python feishu_main.py
   ```

## ✅ 配置验证

运行测试脚本验证配置是否正确：
```bash
venv\Scripts\python.exe scripts\check_llm_config.py
```

输出示例：
```
✅ GITHUB 客户端创建成功!
客户端类型: OpenAI
```

## 📋 各提供商详细说明

### GitHub Models
- **优点**: 免费，无需支付费用
- **获取Token**: https://github.com/settings/tokens
- **支持的模型**: gpt-4o-mini, gpt-4o 等
- **官方文档**: https://docs.github.com/en/github-models

### OpenAI API
- **获取Key**: https://platform.openai.com/api-keys
- **计费**: 按使用量计费
- **支持的模型**: gpt-4, gpt-4o, gpt-3.5-turbo 等

### nVidia API
- **获取Key**: https://build.nvidia.com/
- **优点**: 支持多种开源模型，免费额度充足
- **支持的模型**: meta/llama-3.1-405b-instruct 等

### 自定义API (交大 / 其他兼容OpenAI格式的服务)
- 只需提供 **API Base URL** 和 **API Key**
- 适配任何兼容 OpenAI SDK 的服务

### 本地模型 (Ollama、vLLM 等)
- **优点**: 
  - 🏡 完全离线运行，无隐私泄露
  - ⚡ 无网络延迟，响应更快
  - 💚 完全免费，无费用
- **支持的框架**: 
  - Ollama (推荐)
  - vLLM
  - LLaMA.cpp
  - LocalAI 等任何兼容 OpenAI API 的本地服务
- **安装 Ollama** (推荐):
  ```bash
  # macOS/Linux
  curl https://ollama.ai/install.sh | sh
  
  # Windows 用户从这里下载: https://ollama.ai
  
  # 启动服务
  ollama serve
  
  # 在另一个终端拉取模型
  ollama pull llama2  # 或其他模型
  ```
- **默认 Base URL**: `http://localhost:8000/v1` (Ollama/vLLM)
- **支持的模型**: llama2, mistral, neural-chat, phind-codellama 等
- **API Key**: ✅ **不需要配置**，自动使用占位符 "local"

## 🔄 切换提供商

**只需修改 `.env` 文件中的 `LLM_PROVIDER` 和对应的 API Key，无需修改任何 Python 代码：**

```ini
# 从 GitHub 切换到 OpenAI
LLM_PROVIDER=github       # 改成 openai
GITHUB_TOKEN=xxx...      # 注释掉或留空

LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxx... # 填入 OpenAI API Key
```

## 🛠️ 代码修改说明

本次修改涉及以下文件（**用户只需修改 `.env`，无需关注代码细节**）：

### `brain/llm_brain.py`
- ✅ 新增 `_make_client()` 函数
- ✅ 根据 `LLM_PROVIDER` 自动选择 API 提供商
- ✅ 支持多种 API 兼容格式

### `main.py`
- ✅ 使用 `llm_brain._make_client()` 初始化客户端
- ✅ 移除了硬编码的 GitHub 配置

### `feishu_main.py`
- ✅ 自动使用 `llm_brain.py` 的新配置系统（无需修改）

### `.env`
- ✅ 新增多提供商配置选项
- ✅ 支持 GitHub、OpenAI、nVidia、自定义 API

## 💡 最佳实践

1. **本地测试用 GitHub Models**
   - 免费，配额充足，速度快
   
2. **生产环境按需选择**
   - 对精度要求高 → 使用 OpenAI
   - 成本敏感 → 使用 GitHub/nVidia
   
3. **API Key 安全**
   - `.env` 文件已在 `.gitignore` 中
   - 不要将真实 API Key 提交到仓库

## 🐛 常见问题

**Q: 切换提供商后还是用的旧提供商？**
- A: 确保修改了 `.env` 文件中的 `LLM_PROVIDER`，然后重启程序

**Q: 提示 "API Key 未配置"？**
- A: 检查 `.env` 文件中对应的 API Key 是否填写

**Q: 如何添加新的 API 提供商？**
- A: 在 `brain/llm_brain.py` 的 `_make_client()` 函数中添加新的 elif 分支

---

**✨ 享受灵活的多提供商支持！**
