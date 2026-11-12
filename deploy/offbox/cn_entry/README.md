# 两入口的网页传输约定

`147-182-176-237.sslip.io` 的 HAProxy 只转发 TCP 443，携带 PROXY v2 头到 box A。
TLS 在 box A 的 Caddy 终止；它与 `89-208-253-119.sslip.io` 使用同一应用上游。
这里不建立第二份数据库，也不复制患者数据。

## 缓存与压缩

仓库根 `Caddyfile` 中的策略同时适用于两入口。宿主机的 `/opt/nmu/caddy/Caddyfile`
还有独立的站点地址、监听器、HSTS 与上游设置；移植时只同步以下策略，不能整份覆盖部署配置。

- 仅 GET/HEAD `/assets/<name>-<8位内容hash>.js` 或 `.css` 可以进入优化路径。
- 只有状态 **200** 且响应类型为 `application/javascript`、`text/javascript` 或 `text/css`
  时，才设置 `public, max-age=31536000, immutable`，去掉 `Pragma`，并允许 gzip。
- `Vary: Accept-Encoding` 必须同时出现在 identity 和 gzip 响应中，保留上游其他 Vary 值。
  Caddy 2.11.4 的 encode 只给已编码响应补 Vary，不能单靠 encode 保证 identity 缓存分流。
- HTML、构建号、API、原始录音、非指纹资源、401/404/206、未知 asset 的 200 HTML 兜底，
  均保持 `private, no-store` / `Pragma: no-cache`。
- **304 保守不缓存。** Starlette 的静态 304 可以不带 Content-Type，因此不把仅凭路径或状态
  的响应提升成公共缓存。正常 fresh immutable 资源不需条件请求；强制重载的 304 可能使浏览器
  丢弃旧缓存、下次重新下载 200，这是明确接受的性能取舍。不能为优化 304 放宽 404 或 HTML 门。

`route` 中的顺序必须保留：先声明带响应匹配器的公共 header，再声明 deferred no-store 默认，
然后是内部路径 404、encode 和唯一 reverse_proxy。响应写出时先应用默认，再由合格的 200 JS/CSS
覆盖；改变顺序可能把所有静态响应重新变成 no-store，或使错误响应被长期缓存。
每一种 Content-Type 在 response matcher 中各占一行；同名字段的多个值是 OR。

## HTTP/3

147 入口没有 UDP/QUIC 转发，必须对该 Host 返回 `Alt-Svc: clear`，清除浏览器曾记住的 h3 广告。
89 直接入口保留 Caddy 原有 HTTP/3 协商。不要为解决 147 的 TCP 中转限制而全局关闭直接入口的 h3。
以后新增 TCP 中转域名，须把域名加入同样的精确 Host 规则；只有真实端到端 QUIC 已部署并验收后才移除。

## 验证与回退

发布 CI 用**本次构建并扫描的 Caddy 二进制**运行：

```sh
python scripts/smoke_caddy_release.py --binary /path/to/release/caddy
```

这个测试只开临时回环端口、使用只被测试客户端信任的临时证书。它模拟两域名的 Host/SNI，
核对 gzip 解压字节、identity/gzip 的 Vary、HEAD、两类 ETag 条件请求、200/401/404/206/304、
HTML 兜底及 API/录音不可缓存，并确认 147 清除 h3 而 89 保留。原有 PROXY v2、来源伪造防护和
上传体积限制也必须继续通过；测试不写系统信任库，不联系 ACME，不启动公网监听。

生产变更前保存原配置及权限，并用实际安装的 Caddy 验证候选配置。只 reload **`nmu-caddy.service`**，
不能使用不存在的 `caddy.service`，也不重启 NMU 应用。先检查原配置摘要未漂移；应用后匿名核验两个
入口、正文摘要和缓存/协议头。失败则仅在当前配置仍是原版或候选版时原子还原、reload 并复验；
遇到第三方配置变更应停止自动覆盖。网页缓存政策不需要迁移数据库、删除录音或更改设备配对。

两域名仍属于不同浏览器 origin：登录 cookie、设备绑定、IndexedDB 待上传录音不会自动迁移。
服务端已保存内容共用；患者端更换入口前，应先在原入口确认待上传录音处理完毕。
