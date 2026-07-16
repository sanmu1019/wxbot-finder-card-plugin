# 卡片 XML 字段与触发规则

## 字段来源

机器人协议收到视频号卡片后，消息回调中通常包含原始应用消息 XML。插件会合并：

```text
content
original_content
outer_content
```

不同机器人框架可能使用不同字段名。移植到其他框架时，关键是取得未被截断的
原始卡片 XML。

## 关键字段

最小字段：

```xml
<objectId>OBJECT_ID</objectId>
<objectNonceId>OBJECT_NONCE_ID</objectNonceId>
```

常见辅助字段：

```xml
<finderUsername>FINDER_USERNAME</finderUsername>
<nickname>AUTHOR_NAME</nickname>
<desc>CONTENT_DESCRIPTION</desc>
<feedType>4</feedType>
```

字段映射：

| XML | 插件内部 | Finder 网关 |
|---|---|---|
| `objectId` | `object_id` | `object_id` |
| `objectNonceId` | `object_nonce_id` | `object_nonce_id` |
| `nickname` | `nickname` | 不传 |
| `desc` | `desc` | 不传 |
| `finderUsername` | `username` | 不传 |

`objectId` 与 `objectNonceId` 必须来自同一张卡片。

## 编码处理

实际消息可能包含：

- HTML 实体，例如 `&lt;objectId&gt;`。
- URL 编码，例如 `%3CobjectId%3E`。
- CDATA。
- XML 外层附加文本。
- 标签命名差异，例如 `object_nonce_id`。

插件会：

1. HTML 反转义。
2. 最多执行三轮 URL 解码。
3. 去除 CDATA 包装。
4. 尝试 ElementTree 解析。
5. XML 不完整时使用受限正则兜底。
6. 统一字段名大小写和下划线。

## sph 提取

插件只接受：

```text
https://weixin.qq.com/sph/...
https://mp.weixin.qq.com/sph/...
```

也支持 URL 编码后的 `/sph/SHORT_CODE`。

`objectId` 不能直接当作短链 code。插件要求短链 code 中至少包含字母，避免把纯
数字 object ID 错判为 `sph`。

## 媒体 URL

卡片 XML 可能带：

```text
wxapp.tc.qq.com/.../20302/stodownload
finder.video.qq.com/...
```

这些 URL 可能：

- 缺少 token。
- 已经过期。
- 需要 decodeKey。
- 只对应封面而不是视频。

因此媒体 URL 是后备候选，不是稳定的主链路。

## 默认触发规则

推荐流程：

```text
用户引用视频号卡片
-> 用户发送“解析”
-> wxbot 标记 quoted_link
-> 插件处理引用 XML
```

默认触发词：

```text
解析
解析链接
视频号解析
```

直接发送：

```text
解析 https://weixin.qq.com/sph/SHORT_CODE
```

也可以处理。

## 为什么不默认自动解析

自动处理所有分享消息会带来：

- 群聊误触发。
- 重复下载大文件。
- Finder 私有接口调用频率增加。
- 元宝会话和管理后台登录态压力增加。
- 机器人账号风险扩大。

因此示例配置使用：

```json
{
  "auto_detect": false
}
```

## 移植到其他机器人

其他机器人框架至少需要提供：

```text
原始消息 XML
发送目标 ID
发送文本能力
发送视频或文件能力
```

可复用的核心步骤：

1. 从 XML 提取 `objectId/objectNonceId`。
2. POST Finder 网关。
3. 获取 `data.sph_url`。
4. 可选调用 `wxsph-api`。
5. 下载并发送媒体。

框架事件对象和消息发送 API 需要自行适配。
