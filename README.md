# wxbot 视频号卡片适配插件

这是从现有 wxbot 项目中拆出的独立插件仓库，用于处理机器人收到的微信视频号
卡片、生成 `sph` 短链，并可继续解析和发送视频。

## 先说结论

**机器人微信号协议本身不负责生成 `sph` 短链。**

当前推荐链路是：

```text
机器人协议收到卡片消息
-> 插件读取原始卡片 XML
-> 提取 objectId + objectNonceId
-> Finder 短链网关
-> 视频号管理后台 Chromium
-> 生成 https://weixin.qq.com/sph/...
-> wxsph-api
-> 元宝解析 sph
-> 下载并发送视频
```

机器人协议在这里有两种作用：

1. **必需作用**：把卡片原始 XML 交给插件。
2. **可选作用**：短链网关失败时，调用
   `Finder/GetCommentDetail` 直接刷新媒体 URL 和解密 Key。

第二条路径直接取得媒体，不会生成 `sph`。

## 仓库边界

本仓库包含：

- 可直接安装到现有 wxbot 的 `wxsph_parser` 插件。
- 脱敏的配置示例。
- Windows 本地安装脚本。
- 卡片 XML、接口契约、调用优先级和故障排查文档。
- 卡片字段提取和 Finder 网关 POST 调用测试。

本仓库不包含：

- 微信机器人协议核心。
- Finder 短链网关。
- `wxsph-api` 元宝解析服务。
- 任何账号登录态、Cookie、API Key 或二维码。

## 依赖关系

### 必需

- 兼容的 wxbot 插件框架。
- `requests`。
- 机器人消息上下文能提供原始或引用卡片 XML。

### 推荐

- Finder 短链网关：
  `http://127.0.0.1:8790`
- `wxsph-api` 解析器：
  `http://127.0.0.1:8787`

### 可选

- 带 `/Finder/GetCommentDetail` 的 protocol-core：
  `http://127.0.0.1:9000/api`

## 兼容的 wxbot 结构

插件依赖以下模块：

```text
config.config.Config
core.context.ContextType
core.plugin_system
core.wechat_api.WechatAPIClient
utils.download_helper.download_video
utils.logger.get_logger
```

目标安装位置：

```text
YOUR_BOT_ROOT/
└── wxbot/
    └── plugins/
        └── wxsph_parser/
            ├── main.py
            └── config.json
```

这不是通用的所有微信机器人 SDK 插件。其他框架可以复用字段提取和 HTTP 接口
逻辑，但需要重写消息事件与发送视频部分。

## 本地安装

### 1. 获取代码

```powershell
git clone YOUR_REPOSITORY_URL
cd wxbot-finder-card-plugin
```

### 2. 安装依赖

如果 wxbot 使用独立 Python 环境，在该环境中安装：

```powershell
python -m pip install -r .\requirements.txt
```

### 3. 安装插件

```powershell
powershell -ExecutionPolicy Bypass -File .\install_local.ps1 `
  -BotRoot "E:\path\to\your-bot"
```

安装脚本会：

- 更新 `wxbot/plugins/wxsph_parser/main.py`。
- `config.json` 不存在时，从示例创建。
- 已有 `config.json` 时保留，不覆盖密钥和本地设置。

### 4. 配置

编辑：

```text
YOUR_BOT_ROOT/wxbot/plugins/wxsph_parser/config.json
```

本地推荐配置：

```json
{
  "enabled": true,
  "auto_detect": false,
  "finder_bridge_enabled": true,
  "finder_bridge_base": "http://127.0.0.1:8790",
  "finder_bridge_api_key": "YOUR_FINDER_KEY",
  "api_base": "http://127.0.0.1:8787",
  "api_key": "YOUR_WXSPH_KEY",
  "finder_protocol_enabled": false
}
```

如果机器人 protocol-core 已实现 Finder 详情接口，可打开兜底：

```json
{
  "finder_protocol_enabled": true,
  "finder_protocol_base": "http://127.0.0.1:9000/api",
  "finder_protocol_cgi": 3906
}
```

### 5. 启动依赖服务

先启动 Finder 网关并完成视频号管理后台扫码：

```text
http://127.0.0.1:8790/health
```

确认：

```json
{
  "data": {
    "ready": true
  }
}
```

需要把 `sph` 继续解析成视频时，再启动：

```text
http://127.0.0.1:8787/health
```

### 6. 重启 wxbot

插件由 wxbot 插件管理器加载。安装或修改配置后，需要重启 wxbot。

## 使用方法

默认配置：

```json
{
  "auto_detect": false,
  "triggers": [
    "解析",
    "解析链接",
    "视频号解析"
  ]
}
```

推荐操作：

1. 在微信中引用或回复视频号卡片。
2. 发送 `解析`。
3. 插件读取引用消息中的卡片 XML。
4. 插件生成短链并继续解析。

也可以发送：

```text
解析 https://weixin.qq.com/sph/SHORT_CODE
```

默认不会对所有普通分享消息自动处理，避免机器人误触发和频繁调用私有接口。

## 实际处理优先级

```text
1. 消息中已有 sph
2. objectId + objectNonceId -> Finder 短链网关
3. 可选 protocol-core 直接刷新媒体
4. 尝试卡片中仍有效的 CDN 地址
5. 返回明确错误
```

获得 `sph` 后：

```text
sph -> /api/wxsph -> 摘要和媒体 URL -> 下载 -> 发送视频
```

详细说明见[实际调用链与结论](docs/实际调用链与结论.md)。

## 主要配置

| 配置 | 作用 |
|---|---|
| `auto_detect` | 是否自动处理匹配内容 |
| `triggers` | 手动触发词 |
| `finder_bridge_enabled` | 是否通过 Finder 网关生成 sph |
| `finder_bridge_base` | Finder 网关地址 |
| `finder_bridge_api_key` | Finder 网关 API Key |
| `api_base` | `wxsph-api` 地址 |
| `api_key` | `wxsph-api` API Key |
| `finder_protocol_enabled` | 是否启用协议直刷兜底 |
| `finder_protocol_base` | 机器人协议 API 地址 |
| `send_summary` | 是否发送摘要 |
| `send_video` | 是否下载并发送视频 |
| `video_max_size_mb` | 最大视频体积 |

完整配置见[本地安装与配置](docs/本地安装与配置.md)。

## 测试

安装开发依赖：

```powershell
python -m pip install -r .\requirements-dev.txt
```

运行：

```powershell
python -m pytest -q
python -m py_compile .\plugin\wxsph_parser\main.py
```

测试不会登录微信、不会启动 Chromium，也不会请求真实接口。

## 目录结构

```text
.
├── README.md
├── SECURITY.md
├── install_local.ps1
├── requirements.txt
├── requirements-dev.txt
├── plugin/
│   └── wxsph_parser/
│       ├── main.py
│       └── config.example.json
├── tests/
│   └── test_plugin.py
└── docs/
    ├── 实际调用链与结论.md
    ├── 本地安装与配置.md
    ├── 卡片XML字段与触发规则.md
    ├── 接口契约.md
    └── 故障排查与安全说明.md
```

## 相关独立组件

Finder 短链网关建议作为另一个独立仓库部署：

```text
finder-short-link-gateway
```

两个仓库职责不同：

```text
wxbot-finder-card-plugin
  负责接收消息、提取字段、调用服务、回复用户

finder-short-link-gateway
  负责使用已登录管理后台生成 sph
```

## 许可证

当前仓库未预设开源许可证。上传公开 Git 仓库前，请确认原 wxbot 项目授权范围，
并添加适合的 `LICENSE`。
