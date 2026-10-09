# 中药主图 AI 批量生成项目

这是一个前后端分离的本地 Web 项目：

- 前端：Vue 3 + Vite
- 后端：FastAPI + Uvicorn
- AI 服务：阿里云 DashScope（通义千问图片编辑模型）
- 前端地址：`http://127.0.0.1:5173`
- 后端地址：`http://127.0.0.1:8000`

## 一、运行环境

启动前请确认电脑已安装：

- Node.js 18 或更高版本
- npm
- Python 3
- 可用的 DashScope API Key

可以通过以下命令检查：

```bash
node -v
npm -v
python3 --version
```

## 二、配置 DashScope API Key

任选一种方式即可。

### 方式一：设置当前终端环境变量

```bash
export DASHSCOPE_API_KEY="你的 API Key"
```

该方式仅对当前终端窗口有效，关闭终端后需要重新设置。

### 方式二：保存到本机文件

```bash
printf '%s' '你的 API Key' > ~/.dashscope_api_key
chmod 600 ~/.dashscope_api_key
```

后续启动时会自动读取该文件，不需要重复设置环境变量。

> 不要把 API Key 写入项目代码、提交到 Git，或发送给其他人。

## 三、推荐：一键启动前后端

打开终端，执行：

```bash
cd /Users/leiwanyue/Desktop/batchImage
bash start-demo.sh
```

首次运行时，启动脚本会自动：

1. 在项目根目录创建 Python 虚拟环境 `.venv`；
2. 安装后端 Python 依赖；
3. 安装前端 npm 依赖；
4. 启动后端服务；
5. 启动前端开发服务器。

看到以下提示后，在浏览器打开对应地址：

```text
中药主图 Demo 已启动：http://127.0.0.1:5173
```

访问地址：

```text
http://127.0.0.1:5173
```

停止项目时，在运行项目的终端中按 `Ctrl + C`，脚本会同时关闭前端和后端进程。

## 四、手动分别启动前后端

调试时可以使用两个终端窗口分别启动。

### 1. 启动后端

第一次运行先创建虚拟环境并安装依赖：

```bash
cd /Users/leiwanyue/Desktop/batchImage
python3 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt
```

然后启动后端：

```bash
cd /Users/leiwanyue/Desktop/batchImage/backend
../.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

后端启动后可检查健康状态：

```text
http://127.0.0.1:8000/api/health
```

接口文档地址：

```text
http://127.0.0.1:8000/docs
```

### 2. 启动前端

新开一个终端窗口执行：

```bash
cd /Users/leiwanyue/Desktop/batchImage/frontend
npm install
npm run dev -- --host 127.0.0.1
```

然后访问：

```text
http://127.0.0.1:5173
```

开发环境中，Vite 会把前端发往 `/api` 的请求代理到 `http://127.0.0.1:8000`，因此前后端必须同时运行。

## 五、使用方法

1. 打开前端页面；
2. 为每个产品准备三张图片：罐装主图、药材整体图、药材细节图；
3. 可以逐个选择产品图片，也可以选择包含多个产品子目录的根文件夹；
4. 检查系统自动分配的图片类型，并按需要手动调整；
5. 选择单罐场景、双罐场景或同时选择两种模板；
6. 点击“开始批量 AI 生成”；
7. 等待生成完成后预览或下载图片。

单张上传图片支持：

- PNG
- JPG/JPEG
- WebP
- 最大 20 MB

单批最多处理 10 个产品。前端一次可以导入更多产品，系统会每 10 个产品自动拆分为一批，并在上一批结束后顺序提交下一批。

生成策略：

- 默认同时生成单罐和双罐两个模板；
- 每张效果图需要执行罐体替换和药材替换两个 AI 阶段；
- 页面会在提交前显示预计批次数、效果图数量和 AI 调用次数；
- 服务端全局最多同时执行 2 个生成任务，避免并发过高导致限流；
- PNG、JPEG 和 WebP 上传素材保持原始字节，不缩放、不转码、不压缩；
- 某张图片失败后，可以只重试该图片，也可以重试当前批次的全部失败项；
- 重试不会重新生成已经成功的图片。

生成结果保存在：

```text
/Users/leiwanyue/Desktop/batchImage/output/<任务 ID>/<产品 ID>/
```

## 六、运行测试

后端测试：

```bash
cd /Users/leiwanyue/Desktop/batchImage/backend
../.venv/bin/python -m pytest
```

前端生产构建检查：

```bash
cd /Users/leiwanyue/Desktop/batchImage/frontend
npm run build
```

## 七、常见问题

### 1. 提示“未找到 DashScope API Key”

先执行：

```bash
export DASHSCOPE_API_KEY="你的 API Key"
```

然后在同一个终端中重新执行 `bash start-demo.sh`。

### 2. 页面显示“等待服务配置”

通常是后端没有读取到 API Key。访问 `http://127.0.0.1:8000/api/health`，检查返回结果中的 `api_key_configured` 是否为 `true`。

### 3. 页面没有显示场景模板

检查模板目录中是否存在：

```text
1.png
2.jpg
```

也可以访问健康检查接口，查看 `missing_templates` 返回了哪些缺失文件。

### 4. 端口已被占用

项目默认使用：

- 前端端口：`5173`
- 后端端口：`8000`

请先关闭占用这些端口的旧进程，再重新启动项目。

### 5. 前端能打开，但提交时提示请求失败

确认后端终端没有报错，并检查：

```text
http://127.0.0.1:8000/api/health
```

如果该地址无法访问，说明后端尚未正常启动。

### 6. 依赖安装失败

确认网络正常，然后分别重试：

```bash
cd /Users/leiwanyue/Desktop/batchImage
./.venv/bin/pip install -r backend/requirements.txt
npm install --prefix frontend
```
