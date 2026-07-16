# 安全说明

## 禁止提交

不要提交：

```text
plugin/wxsph_parser/config.json
真实 API Key
Cookie、token、session key
机器人二维码
机器人 wxid
完整 objectNonceId
完整卡片 XML
视频下载缓存
运行日志
```

仓库只保留 `config.example.json` 占位配置。

## 日志约束

允许记录：

```text
objectId
objectNonceId 长度
接口状态码
媒体候选数量
耗时和错误类型
```

禁止记录：

```text
完整 objectNonceId
协议请求头
微信登录态
API Key
完整带 token 的 CDN URL
decodeKey
```

## 密钥泄漏

发现 Finder 网关 Key 或解析器 Key 泄漏后：

1. 立即更换服务端 Key。
2. 修改插件 `config.json`。
3. 重启 wxbot。
4. 检查日志、聊天记录和 Git 历史。

## 对外服务

插件本身不提供公网端口。Finder 网关和 `wxsph-api` 如需跨机器调用，应使用
HTTPS、独立 API Key、限流和来源限制。

不要把机器人协议端口直接暴露到公网。
