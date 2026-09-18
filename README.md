# local_rss

用 [Kimi WebBridge](https://www.kimi.com/products/kimi-webbridge) 在**真实浏览器**里抓取动态，生成 RSS。

因为是在你自己的浏览器里带着登录态发请求，所以不需要处理 cookie / 验证码 / 风控，
也不受「必须用无头浏览器」的限制。站点通过 `config.yaml` 配置，新增站点只要加一个 provider 文件。

当前支持：

| 站点 | `type` | 说明 |
|------|--------|------|
| B 站 | `bilibili` | 关注动态（`https://t.bilibili.com/`）或指定 UP 主空间动态 |
| 知乎 | `zhihu` | 个人主页动态（`https://www.zhihu.com/people/<token>`） |

## 前置条件

1. 安装并启动 Kimi WebBridge，且浏览器扩展处于连接状态：
   ```bash
   ~/.kimi-webbridge/bin/kimi-webbridge status   # 需要 running + extension_connected 均为 true
   ```
2. 在浏览器里**已登录** B 站 / 知乎。
3. Python 3.10+ 和 PyYAML：
   ```bash
   pip install -r requirements.txt
   ```

## 快速开始

```bash
cd /Users/yangqian/Downloads/local_rss
python3 rss.py                 # 处理 config.yaml 里全部启用的源
python3 rss.py zhihu-kvxjr369f # 只处理指定源
python3 rss.py --list          # 列出配置里的源
python3 rss.py --list-types    # 列出已支持的站点类型
```

每次运行的结果：

```
[bilibili-follow] 打开页面 https://t.bilibili.com/
[bilibili-follow] 新增 57 条，共 57 条 -> output/bilibili-follow.xml
[zhihu-kvxjr369f] 打开页面 https://www.zhihu.com/people/kvxjr369f
[zhihu-kvxjr369f] 新增 35 条，共 35 条 -> output/zhihu-kvxjr369f.xml
```

- 第一次运行会把能翻到的页都抓下来（受 `max_pages` 限制）。
- 之后是增量的：从第 1 页开始，**整页都是已有记录就停止翻页**，通常几秒钟结束。
- 抓取时会在浏览器里打开对应页面（归在 `bridge.group_title` 这个标签组下），
  跑完会自动关掉，详见[标签页清理](#标签页清理)。

## 手动操作：main.sh

日常跑定时任务就够了，要手动干预时用 `main.sh`（它内部走 `run.sh`，
所以会先确保 WebBridge daemon 在跑）：

```bash
./main.sh                  # 增量刷新（全部源）
./main.sh force            # 强制刷新（见下）
./main.sh zhihu-kvxjr369f  # 只跑指定源
./main.sh force bilibili-follow   # 组合
./main.sh clean            # 关掉遗留的浏览器标签页
./main.sh ping             # 只 ping hub 通知有更新（不抓取，用来验证 WebSub 链路）
./main.sh status           # pm2 进程 / 订阅地址 / 数据概览
./main.sh log 50           # 最近 50 行抓取日志
./main.sh restart          # 重启 pm2 抓取任务（等于立刻跑一次）
```

> 被过滤掉的条目（命中了哪个关键词 / 被哪条规则丢的）在 `logs/<feed-id>.dropped.log`
> 里，每次运行覆盖写，详见「过滤与去重规则 · 过滤清单」。

`status` 大概长这样：

```
== pm2 进程 ==
│ 6  │ local-rss         │ ... │ stopped │
│ 7  │ local-rss-http    │ ... │ online  │

== 订阅地址 ==
  本机    http://127.0.0.1:8666/<feed-id>.xml
  tailnet http://100.117.207.33:8666/<feed-id>.xml

== 输出 ==
  bilibili-follow.xml            34 条   09-12 23:31    48K
  zhihu-kvxjr369f.xml            26 条   09-12 23:31   184K

== 状态 ==
  bilibili-follow.json           58 条   updated ...
  zhihu-kvxjr369f.json           35 条   全文 26 条   updated ...
```

### 强制刷新（`./main.sh force`）

| | 行为 | 耗时 |
|---|---|---|
| 普通刷新 | 从第 1 页开始，整页都是已有记录就停 | ~7s |
| `force` | 假装本地什么都没有，翻满 `max_pages` 重抓一遍并重新渲染 | ~9s |

实测抓到的条数：bilibili **57 vs 20**、zhihu **35 vs 7**。

什么时候需要 force：

- 改了**渲染逻辑**（标题格式、头像、播放器这类），想让存量条目也更新 ——
  因为 `content` 是存在 state 里的，不会自动重算。
- 想重新走一遍完整的列表抓取。

什么时候**不需要**：

- 调过滤器、关键词、去重规则 —— 这些每次运行都作用在完整列表上，普通刷新就够了。

`force` **不会清空 state**，知乎已抓好的全文不会丢。

### 彻底重来

```bash
rm state/*.json && ./main.sh
```

会重新抓全量。代价：知乎全文（`content_locked`）一起没了，需要按
`max_fulltext_per_run` 分批重新补齐；跨窗口的去重指纹（`seen_content`）也没了，
已经发过的正文可能被重发一次（一次运行就会重新攒回来）。

## 改了配置多久生效

| 改的东西 | 生效方式 |
|----------|----------|
| `exclude_keywords` | **立即** |
| `filter_self_repost` / `filter_image_only` | **立即** |
| `dedupe_by_content` / `dedupe_memory_days` | **立即** |
| `output.dropped_log` | 下次运行（只是多写一份过滤清单，不影响 RSS 内容） |
| `show_avatar` / `embed_player` | **立即** |
| `image_digest_size` / `image_digest_max_age_hours` | 下次运行。**正文会跟着重渲染，但已经分好的批次不会重排** —— 要重来就删掉 state 里那些 `extra.kind == "image_digest"` 的条目 |
| `max_pages` / `fulltext` / `max_fulltext_per_run` | 下次运行 |
| 标题格式、正文结构这类**渲染逻辑**的改动 | 要 `./main.sh force` |

前四类之所以立即生效，是因为它们每次运行都作用在「state 全量条目」上。
`show_avatar` 和 `embed_player` 也是——它们不写在抓取时渲染的 `content` 里，
而是在 `postprocess` 里**先摘掉旧的、再按当前开关加回去**（幂等），
所以改开关不用重抓：

```
show_avatar: false -> 头像 0 个
show_avatar: true  -> 头像 34 个     （同一份 state，未重抓）
show_avatar: false -> 头像 0 个
```

最后一类（标题格式之类）改的是抓取时渲染的结果，而 `content` 存在 state 里，
所以只能靠 `./main.sh force` 重抓重渲。

## ⚠️ 阅读器那一侧的缓存

RSS 阅读器是**按 `<guid>` 增量收录**的：guid 没变，它就不会去更新已经收过的条目内容。
我们的 guid 里没有「抓取时间」这类每次都变的东西（B 站是 `bilibili:<动态id>`，
知乎是 `zhihu:<发布时间>_<编辑时间>_<哈希>`），
所以**改了渲染结果后，阅读器里已经存在的条目还是显示旧内容** —— 这跟本地 XML 无关。

想在看过的阅读器里看到新内容，得在阅读器里**删掉这个订阅源再重新添加**。
（FreshRSS：订阅管理 → 删除 → 重新订阅同一个地址。）

这次的播放器就是典型：本地 XML 已经是移动版播放器了，但手机上如果还是旧内容，
那多半是阅读器缓存，不是播放器本身的问题。

> 例外是知乎的**编辑**：编辑时间是要编进 guid 的，所以内容被编辑后 guid 跟着变，
> 阅读器会当成新条目收下来 —— 这一条正是为了绕开上面这个缓存规则，见「zhihu · 编辑」。

## 订阅

订阅地址（由 pm2 里的 `local-rss-http` 常驻提供）：

```
http://127.0.0.1:8666/bilibili-follow.xml
http://127.0.0.1:8666/zhihu-kvxjr369f.xml
```

它就是个静态服务，直接读 `output/` 目录，所以抓取任务刷新文件后订阅者拿到的是最新内容，
不用重启这个服务。也支持 `If-Modified-Since`（返回 304），阅读器轮询不会白拉数据。

端口和绑定地址在 `ecosystem.config.js` 的 `local-rss-http.env` 里改，改完
`pm2 restart local-rss-http && pm2 save`。

默认绑 `127.0.0.1`，只有本机能访问。**想让手机/平板订阅**就把 `HOST` 改成 `0.0.0.0`，
订阅地址换成这台机器的局域网 IP（`ipconfig getifaddr en0`）。
注意那会把 RSS 暴露给同局域网的所有设备 —— 这个 feed 里有你关注谁、收藏了什么。

`config.yaml` 里的 `output.base_url` 要和这个端口（以及你**实际用来访问的地址**）一致，
它决定 RSS 内部的 `<atom:link rel="self">`：

```yaml
output:
  base_url: http://100.117.207.33:8666
```

（这里原先是 `127.0.0.1`，开了 WebSub 之后改成了 tailnet 地址：FreshRSS 拿这个 self
地址当 WebSub 的 topic，hub 也得能抓它，而 `127.0.0.1` 对别的机器没有任何意义。）

想临时手动起一个（不用 pm2）也可以：

```bash
cd /Users/yangqian/Downloads/local_rss/output && python3 -m http.server 8666
```

### 从 Tailscale 上的其他设备订阅

服务本身绑在 `127.0.0.1`，tailnet 里的设备访问不到，
所以用 `tailscale serve` 把它转发到 tailnet（本地绑定不用改）：

```bash
# 一次性配置；--bg 表示后台常驻，配置持久保存在 tailscaled 里
tailscale serve --bg --tcp=8666 8666

tailscale serve status            # 查看
tailscale serve --tcp=8666 off    # 关掉
```

订阅地址（需要该设备登着同一个 tailnet）：

```
http://100.117.207.33:8666/bilibili-follow.xml          # tailnet IP
http://huygens:8666/bilibili-follow.xml                 # 节点名（需 MagicDNS）
http://huygens.taild6a905.ts.net:8666/bilibili-follow.xml   # 完整 MagicDNS 名
```

三个地址实测都是 200，`If-Modified-Since` 返回 304，FreshRSS 之类的阅读器都能正常拉。

> ⚠️ **必须用 `--tcp`，不要用 `--http`。**
>
> `tailscale serve --http=8666 8666` 走的是 Tailscale 的 HTTP 代理层，它按 **Host 头** 路由：
>
> | 请求 | Host 头 | 结果 |
> |---|---|---|
> | `http://huygens.taild6a905.ts.net:8666/...` | 名字 | 200 |
> | 同一地址 + `-H "Host: 100.117.207.33:8666"` | IP | **404** |
>
> 同一个地址、只是 Host 不同结果就变了。用裸 IP 订阅会拿到一个 Go 风格的
> `404 page not found`（不是 Python http.server 那个 HTML 404 页面），
> FreshRSS 会报 `cURL error 22: The requested URL returned error: 404`。
>
> `--tcp` 是**裸 TCP 转发**，不做任何 Host/SNI 路由，所以上面三个地址一视同仁全部可用。

仍然只有 tailnet 内的设备能访问：本机绑定是 `127.0.0.1`，局域网 IP 直接访问连不上，
也没有开 Funnel（`tailscale funnel status` 应显示同样的 tailnet only 项，没有 "Funnel on"）。

Tailscale 挂了不影响本机使用；要改成绑 tailnet 网卡（不用 serve）也可以，
但那样在 Tailscale 未连上时 `--bind` 会失败，不如 `serve --tcp` 稳。

```js
// ecosystem.config.js → local-rss-http.env（备选做法，不推荐）
HOST: '0.0.0.0',          // 绑所有网卡：tailnet 和局域网都能访问
```

`config.yaml` 里的 `output.base_url` 决定 RSS 内部的 `<atom:link rel="self">`，
不影响抓取；如果阅读器不介意，留 `127.0.0.1` 也没问题。
**但开启 WebSub 后它还会被当成「feed 的地址」告诉 hub**，那时就必须填订阅者用的那个地址（见下）。

## WebSub（更新推送）

默认是阅读器**自己定时来拉** feed（FreshRSS 一般 1 小时一次），所以就算抓取每 30 分钟跑一次，
手机上也可能要等一小时才看到。WebSub 把它改成「有更新就推」：feed 里声明一个 hub，
我们抓完发现有新条目就 ping 一下 hub，hub 立刻来抓 feed 并推给所有订阅者。

### 怎么开

在 `config.yaml` 的 `output` 下填 hub 地址即可（单个源可以用 `hub` 键覆盖）：

```yaml
output:
  base_url: http://100.117.207.33:8666   # 订阅地址，也是通知 hub 时用的 feed 地址
  hub_url: http://100.117.207.33:8667/   # hub 地址；留空 = 关闭
```

开了之后：

- `output/*.xml` 会多一行 `<atom:link href="..." rel="hub"/>`，阅读器订阅时据此知道该去哪个 hub 注册；
- 每次抓取**有新条目**时自动 ping 一次 hub（没新内容那种常见运行就不打扰它）；
- 留空则一切照旧，RSS 里也不会出现 hub 声明。

### ⚠️ hub 必须能自己抓到 feed

hub 是主动方：收到通知后它要去 fetch feed 拿内容，再推给订阅者。所以三个方向都得通：

| 谁 | 要连谁 | 前提 |
|---|---|---|
| hub | 你的 feed（`base_url` 那个地址） | feed 对 hub 可达 |
| 阅读器 | hub（订阅时注册回调地址） | hub 对阅读器可达 |
| hub | 阅读器（推送回调，FreshRSS 是 `p/api/`） | 阅读器的 `base_url` 对 hub 可达 |

**公共 hub 配不上这套**：`https://websubhub.com/`、`https://pubsubhubbub.appspot.com/`
都是公网服务，抓不到 `100.117.207.33` 这种 tailnet 地址。要让它们能用，得先
`tailscale funnel --bg --tcp=8666 8666` 把 feed 暴露到公网 —— 那等于把你关注谁、
收藏了什么公开出去，别这么干。

**自建 hub 才是对的路子**，而这个项目里就带了一个：`localrss/hub.py`，跑成 pm2 的
`local-rss-hub`（见 `hub.sh`）。纯标准库实现，不做重试、不做多余花活，够用就行。
它跑在这台机器上，三方全在 tailnet 内闭环：

```
FreshRSS ──① 订阅 ──▶ http://100.117.207.33:8667/   （本机 hub）
   ▲                          │
   │                     ② 抓 feed
   │                          ▼
   └──③ 推送内容──── http://100.117.207.33:8666/*.xml（本机 feed 服务）
```

hub 本身只需要本机可达，对 tailnet 的暴露沿用 feed 那一套：

```bash
tailscale serve --bg --tcp=8667 8667    # 一次性配置，之后一直有效
tailscale serve --tcp=8667 off          # 关掉
```

订阅关系存在 `state/hub.json`，重启不丢；打开 `http://100.117.207.33:8667/` 能看到
当前有哪些订阅、租期还剩多久。hub 的日志在 `pm2 logs local-rss-hub`。

FreshRSS 那边（`data/config.php`）：

```php
'base_url' => 'http://47.120.35.57:8080/',   // 这台实例实际的 base_url
'pubsubhubbub_enabled' => true,
```

只有 `base_url` 看起来是公网地址、**或者 hub 和 `base_url` 同主机**时它才会启用 WebSub
（`pubSubHubbubPrepare()` 里的判断）。而 `serverIsPublic()` 只排除 RFC1918 那几个网段，
**不认 `100.64.0.0/10`** —— tailnet 的 `100.x` 地址在它眼里就是「公网」。这台实例
`base_url` 又正好是公网 IP（`47.120.35.57:8080`），所以 WebSub 会正常启用。

### FreshRSS 侧的实测情况（1.26.0）

进容器里直接看过，**不需要改任何配置**：

```php
'base_url' => 'http://47.120.35.57:8080',   // 公网 IP，serverIsPublic() 判定为 true
'pubsubhubbub_enabled' => true,             // 本来就开着
```

- 4 个源在 `feed` 表里登记的 `url` 就是 `http://100.117.207.33:8666/<id>.xml`，
  和我们生成的 `rel="self"` 一致，所以 WebSub 的 topic 天然对得上；
- **1.26.0 里没有那道 SSRF 黑名单**：`100.64.0.0/10`、`internal_host_allowlist`、
  `PRIVATE_SUBNETS` 在整个仓库里都搜不到。所以它抓 `100.x` 的 feed、POST 到 `100.x`
  的 hub，都不会被拦。

> ⚠️ 以后**升级 FreshRSS 要注意**：新版（edge / 1.27+）加了 `app/Utils/httpUtil.php`，
> 里面的 `PRIVATE_SUBNETS` 明确包含 `100.64.0.0/10`。升上去之后就得在
> `data/config.php` 里补一行，否则 feed 和 hub 会一起被拦：
>
> ```php
> 'internal_host_allowlist' => ['100.64.0.0/10'],
> ```
>
> 用 CIDR，别写成带端口的 `100.117.207.33:8666` —— hub 在 8667 上。
> 症状是 `log_pshb.txt` 里出现
> `Fetching this URL is not allowed, because the host's IP is not in the allowlist`。

**什么时候会订阅**：FreshRSS 的每源刷新间隔是 `ttl_default = 3600`（一小时），
所以 feed 里多了 hub 声明之后，要等这个源下一次真正被拉取才会被发现。容器自己的
cron 是 `CRON_MIN=1,31`（每小时 :01 和 :31 跑一次 `app/actualize_script.php`），
但每轮只刷新「距上次更新超过 1 小时」的源 —— 手动跑 `actualize_script.php` 也一样会被
TTL 跳过。想立刻生效，就在 FreshRSS 界面上对这几个源点一下刷新。

本实例已经把这 4 个源的 `ttl` 从 `0`（= 继承 `ttl_default` 3600）改成 **`900`**，
所以每轮 cron 都会刷。要改回去：

```sql
UPDATE feed SET ttl = 0 WHERE url LIKE 'http://100.117.207.33:8666/%';
```

> 顺带发现的另一件事：FreshRSS 抓每个源的超时是 **20 秒**（日志里
> `cURL error 28: ... after 20001 milliseconds`），而这几个 feed 有 150~420 KB，
> 走 tailnet 偶尔会超时（实测 4 个里有 2 个中招，下一轮就正常了）。超时的源会被标
> `error=1` 并重试，**和 WebSub 无关** —— 那个标记只表示「这次抓取失败」。
> 真嫌它烦，可以给这几个源单独加大 curl 超时。

订阅是 FreshRSS 自己发起的：它每次刷新 feed 都会重读一遍 hub 声明，发现同时有
`rel="hub"` 和 `rel="self"` 就自动往 hub 订阅（`feedController.php` 里的
`pubSubHubbubPrepare()` / `pubSubHubbubSubscribe()`），**不需要手动操作**。之后：

- 订阅成功 → hub 立刻推一次当前内容 → 它把 WebSub 标成可用；
- 每次抓取有新条目 → 我们 ping hub → hub 抓 feed → 推给 FreshRSS → **秒收**；
- 如果某次是它自己轮询才发现新文章（说明推送没生效），它会打 warning 并暂时退回
  普通轮询，约 23 小时后重试订阅 —— 所以配好后隔一轮刷新去看日志最靠谱。

这些情况都记在 `./FreshRSS/data/users/_/log_pshb.txt`。

### 验证

```bash
./main.sh ping              # 只通知 hub，不抓取，不需要浏览器
./main.sh ping zhihu-nell   # 只通知指定源
```

```
[bilibili-follow] 已通知 hub（HTTP 204）：http://100.117.207.33:8666/bilibili-follow.xml
```

204 就是「收到了」。地址写错或 hub 没起来只会往 stderr 打一条 `WebSub 通知失败：…`，
**不影响抓取本身**，也不会让定时任务算失败。日常抓取里有新条目时会自动做同样的事。

FreshRSS 文档里推荐的那几个在线测试服务在这儿**基本用不上**：websub.rocks 和
test.livewire.io 都是公网服务，得能自己访问到你的 hub / topic，只绑在 tailnet 上的 hub
它们够不到。（`push-tester.cweiske.de` 官方文档只提了一句，具体用法没查到，不指望它。）

所以验证就靠本地这几条：

```bash
curl http://100.117.207.33:8667/     # hub 上登记了哪些订阅，应该能看到 FreshRSS 的 pshb.php
./main.sh ping                       # 手动触发一次通知，不需要浏览器
pm2 logs local-rss-hub               # hub 侧的验签 / 抓取 / 推送全过程
```

排查顺序：

| 现象 | 多半是 |
|---|---|
| hub 日志里完全没有验签记录 | FreshRSS 还没订阅 —— 先确认它在抓 feed、没被 SSRF 拦住、feed 里确实有 `rel="hub"` |
| 有验签，但推送一直失败 | 它登记的回调地址 hub 访问不到（看日志里那串 `api/pshb.php?k=…`） |
| `./main.sh ping` 报「连不上 hub」 | hub 没起来，或 `hub_url` 写错 |
| hub 日志里说「抓 feed 失败」 | `curl <base_url>/<feed-id>.xml` 试一下 —— 抓不到这个地址，后面全都是白搭 |

## 标签页清理

抓取需要真实页面来带登录态，所以每次运行都会新开标签页（归在 `bridge.group_title` 这个标签组下）。
跑完（**包括中途失败**）会自动关掉本次开的标签页：

```
[清理] 已关闭本次打开的 2 个浏览器标签页
```

只会关掉本 session（`bridge.session`）里的标签页，**不会碰你自己已经打开的 B 站/知乎页面**：
脚本从不复用你手开的页面（`find_tab` 默认只在本 session 内查找，也不会借用你当前正在看的标签页），
只用 `newTab` 新开自己的；收尾的 `close_session` 也只清空本 session 的标签组。

### 什么时候会漏

清理挂在 `finally` 上，所以正常退出、报错、`Ctrl-C`、`pm2 stop/restart`（发 SIGINT）都会走到。
**但 SIGTERM 是个坑**：Python 默认收到 SIGTERM 会直接终止，`finally` 根本不执行。
关机、`kill <pid>`、launchd 停服务走的都是 SIGTERM。

已修：脚本启动时装了 SIGTERM handler，转成中断信号走同一条清理路径。

```
# 对照实验（不加 handler 的情况）
kimi$ python3 sigdemo.py & sleep 0.5; kill $!
exit = -15        # 直接死掉，finally 里的清理被跳过
```

### 手动兜底

漏了（比如进程被 `kill -9`）跑一条命令就能清掉本 session 的残留：

```bash
python3 rss.py --clean
# [清理] session 'local-rss' 关闭了 2 个标签页
```

注意它只能清掉 **daemon 还记得的** 标签页。如果浏览器扩展在中途重载/重启过，
session→tab 的映射会丢，那些标签对 daemon 来说就成了孤儿，`list_tabs` 看不到、
`close_session` 也够不着 —— 只能手动在浏览器里关掉。

想保留标签页方便调试：

```bash
python3 rss.py --keep-tabs                            # 本次临时保留
```

或在 config 里长期关掉：

```yaml
bridge:
  close_session: false
```

## 定时刷新（持久化）

pm2 管三个进程：

| 进程 | 作用 | 特性 |
|------|------|------|
| `local-rss` | 定时抓取 | 跑完就退出，`cron_restart` 到点重跑，`pm2 list` 里显示 `stopped` 是正常的 |
| `local-rss-http` | 常驻订阅服务 | `autorestart: true`，挂了自动拉起 |
| `local-rss-hub` | 常驻 WebSub hub | 同上；只服务自己的几个 feed，日志看 `pm2 logs local-rss-hub` |

**刷新间隔写在 `ecosystem.config.js` 的 `cron_restart` 里，不是 `config.yaml`。**
（`config.yaml` 只管抓什么源；多久跑一次是调度器的事。）

```bash
cd /Users/yangqian/Downloads/local_rss
pm2 start ecosystem.config.js     # 启动（抓取任务会立刻跑一次）
pm2 save                          # 存下进程列表（开机恢复靠它）
pm2 list                          # 看两个进程的状态
pm2 logs local-rss                # 抓取日志
pm2 logs local-rss-http           # 订阅服务日志
pm2 restart local-rss             # 手动立刻抓一次
pm2 restart local-rss-http        # 改了端口后重启订阅服务
pm2 delete local-rss local-rss-http
```

如果这台机器还没配过 pm2 的开机自启，跑一次 `pm2 startup` 并按它给出的提示执行
（会写一个 launchd agent，让登录时自动 `pm2 resurrect`），然后再 `pm2 save`。

> ⚠️ **macOS 13+ 还有一道系统设置要过。** `pm2 startup` 只是写了个 plist，
> 系统仍可能把它标成 `disallowed`，那样登录时根本不会执行。查一下：
>
> ```bash
> sfltool dumpbtm | grep -B6 -A6 "com.PM2"
> #   Disposition: [enabled, allowed, ...]      ← 正常
> #   Disposition: [enabled, disallowed, ...]   ← 被挡住了，登录不会跑
> ```
>
> 是 `disallowed` 就去 **系统设置 → 通用 → 登录项 → 允许在后台**，
> 找到对应的条目（pm2 这个会显示成 `sh`，因为 `Executable Path` 是 `/bin/sh`）打开开关。
> `sfltool` 没有改单项的 CLI，只有 `resetbtm` 全量重置（会清掉所有后台项授权，别用）。
>
> 注意这影响的是**整个 pm2 daemon**：`com.PM2` 不跑，里面所有应用重启后都不会自己起来。

改间隔：编辑 `ecosystem.config.js` 里的 `cron_restart`，然后

```bash
pm2 restart local-rss --update-env   # 或 pm2 delete local-rss && pm2 start ecosystem.config.js
pm2 save
```

几个必须知道的点：

- **`autorestart: false` 不能去掉**。这是「跑完就退出」的一次性脚本，
  去掉后 pm2 会把正常退出当成崩溃，无限重启刷屏。
- 跑完在 `pm2 list` 里显示 **`stopped` 是正常的**，等 `cron_restart` 到点会自己再跑。
  实测确认过：进程处于 `stopped` 状态时 cron 依然照常触发。
- `run.sh` 会补 PATH、确保 WebBridge daemon 在跑，所以 pm2 不需要额外的环境配置。

### 另外两种方式（备选）

`deploy/com.localrss.refresh.plist` 是 launchd 版本（改 `StartInterval`，单位秒）：

```bash
cp deploy/com.localrss.refresh.plist ~/Library/LaunchAgents/
# bootstrap 是现在的推荐做法；launchctl load 是遗留子命令
# （launchctl help 里它自己写着 "Recommended alternatives: bootstrap | enable"）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.localrss.refresh.plist
launchctl kickstart -k gui/$(id -u)/com.localrss.refresh   # 立刻跑一次
```

> 别用 `launchctl list | grep <label>` 判断有没有加载成功。
> 带 `LaunchOnlyOnce` 的作业（`pm2 startup` 生成的就是）跑完会被 launchd 立刻从 domain 移除，
> 日志里就是这样三步：`Setting service ... to enabled` → `service inactive` → `removing service`。
> 所以 `list`/`print` 查不到是正常的，**不代表失败**。
> 想确认它到底跑没跑，看它产生的输出（日志文件有没有增长）最靠谱。

cron 版本：

```cron
*/30 * * * * cd /Users/yangqian/Downloads/local_rss && ./run.sh >> logs/refresh.log 2>&1
```

三种方式都统一走 `run.sh`，所以换调度器不用改代码。

## 抓取间隔怎么定

- **推荐 30 分钟**。动态类信息源半小时的延迟完全够用，
  而每次增量运行只发 1～2 个请求、**实测约 1 秒**结束，开销可以忽略。
- 15 分钟也行；**不建议低于 10 分钟** —— 收益很小，但会明显增加被平台风控盯上的概率。
- 别把两个「刷新」搞混：这个间隔是**脚本抓源站的频率**；
  RSS 阅读器多久拉一次 `output/*.xml` 是它自己的事（一般 1 小时），两者互不影响。
- 每次运行都是增量的：从第 1 页开始，整页都是已有记录就停，所以间隔调密也不会累积负担。

## config.yaml

```yaml
bridge:
  url: http://127.0.0.1:10086/command   # WebBridge daemon 地址
  session: local-rss                    # 标签页所属 session（标签组）
  group_title: 本地 RSS                 # 标签组显示名
  close_session: true                   # 跑完关掉本次打开的标签页

output:
  dir: output        # RSS 输出目录（相对 config.yaml 所在目录）
  state_dir: state   # 去重/增量用的状态目录
  log_dir: logs      # 过滤清单的目录（见下面的 dropped_log）
  dropped_log: true  # 每轮把被过滤掉的条目写成 <log_dir>/<feed-id>.dropped.log（覆盖写）
  history: 150       # 每个源最多保留多少条（可被各 feed 的 history 覆盖）
  base_url: ""       # 可选，服务地址前缀（开了 WebSub 后就是 hub 抓 feed 用的那个地址）
  hub_url: ""        # 可选，WebSub hub 地址；留空 = 关闭（见「WebSub」一节）

# 全局关键词过滤：标题或正文命中任一关键词的条目会被丢掉。
# 对所有 feeds 生效，不想要就清空这个列表。
# 只想拦某个源的，就写在那个 feed 自己的 exclude_keywords 里（见下），别放这里。
exclude_keywords:
  - 妙界
  - 互动抽奖
  - 捞一下

feeds:
  - id: bilibili-follow        # 唯一 ID，决定 output/<id>.xml 和 state/<id>.json
    type: bilibili             # 对应已注册的 provider
    title: B站 · 关注动态       # RSS channel 标题
    description: ...           # RSS channel 描述
    site_url: https://t.bilibili.com/   # RSS channel link
    file: bilibili-follow.xml  # 可选，自定义输出文件名
    enabled: true              # 可选，false 则跳过
    history: 150               # 可选，覆盖 output.history（按源单独设上限）
    exclude_keywords:          # 可选，只对本源生效，叠加在顶层的全局列表之上
      - 关于每天的早安行动
    options:                   # provider 各自的参数
      mode: feed
      max_pages: 3
      filter_self_repost: true
```

### bilibili 的 options

| 键 | 默认 | 说明 |
|----|------|------|
| `mode` | `feed` | `feed`：关注动态；`space`：指定 UP 主空间动态 |
| `uid` | — | `mode: space` 时必填，UP 主的 UID |
| `max_pages` | `3` | 最多翻几页，每页约 20 条 |
| `filter_self_repost` | `true` | 过滤掉「转发自 @自己」的回环转发 |
| `image_digest_size` | `0` | 攒够这么多条「图片动态 + 转发」就合成一条合集（`0` = 关掉，见下） |
| `image_digest_max_age_hours` | `24` | 攒不满的兜底有效期，小时；`0` = 不兜底 |
| `filter_image_only` | `true` | 过滤掉纯图动态。**`image_digest_size > 0` 时本项不生效**（纯图走合集） |
| `fulltext` | `true` | 把被平台截断的正文补成全文（见「bilibili · 全文」） |
| `max_fulltext_per_run` | `10` | 每次运行最多补几篇全文 |
| `show_avatar` | `true` | 在每条正文开头放 UP 主头像（改开关立即生效） |
| `embed_player` | `html5player` | 视频内嵌播放器：`mobile` / `html5player` / `desktop` / `newplayer` / `false`（改开关立即生效） |

### bilibili 的标题格式

投稿视频的标题是 **`作者 + 空格 + 视频标题`**，不带动作前缀：

```
苹果箱AppleBoxFilm 你不是混的很好吗？钱呢！
鞑厨高寒 土豆的国外花样
```

这样光看标题就知道是谁发的（很多阅读器不会把 `dc:creator` 显示在列表里）。
其余类型保持原样：`发布了文章：…`、转发取转发的正文首行、纯文字动态取正文首行，
**图文动态取正文（`opus.summary`）首行**，实在没正文又没标题的才叫 `发布了 N 张图片`。
**「更新了合集」（`DYNAMIC_TYPE_UGC_SEASON`）也走视频这条路**
（标题同样是 `作者 + 视频标题`，正文里那行写成 `合集更新：`）。

```
最近GTA5最大私服社区NopixelV开始内测了
《可能的爱情》获威尼斯电影节评审团大奖。
想与各位分享一个喜讯，影视飓风出品的《鸽环》，在第83届威尼斯国际电影节获得了沉浸式单元大奖！
```

（图片动态以前这里全是 `发布了 N 张图片`，原因是接口少传了参数，见
「图文动态的正文哪去了」。）

### zhihu 的标题格式

和 B 站保持一致，也是 **`作者 + 空格 + 标题`**，动作前缀去掉：

```
金错刀 良子背后的摇钱树，2026惨遭资本抛弃
夏天 为什么我觉得生物学完全缺乏作为理科的严谨?
q9adg 房车博主大批消失，床车自驾爆火，二者差距到底有多大？
```

原来长这样：`收藏了回答：…` / `回答了问题：…` / `发布了想法：…`。
**动作没丢**，还在正文第一段里（`<p>收藏了文章</p>`），只是不再占标题。

> ⚠️ 标题是**存在 `state/<id>.json` 里**的，不是每次输出时重新生成。
> 所以改标题格式后，已经存进 state 的旧条目不会自动跟着变——
> 增量抓取只会刷新第 1 页。要让全量生效，两条路：
>
> - `./main.sh force`（重抓重渲，保留 state 和知乎全文）；
> - 或者删掉 `state/<id>.json` 强制全量重抓（会丢掉已滚出 `max_pages` 窗口的旧条目，
>   以及知乎已抓好的全文）。

### zhihu 的 options

| 键 | 默认 | 说明 |
|----|------|------|
| `token` | — | 主页 URL 里的 token，如 `https://www.zhihu.com/people/kvxjr369f` → `kvxjr369f` |
| `max_pages` | `5` | 最多翻几页 |
| `dedupe_by_content` | `true` | 正文相同的只保留一条（跨窗口，见下） |
| `dedupe_memory_days` | `45` | 去重指纹记多久（0 = 只比窗口内的条目） |
| `fulltext` | `true` | 导航到内容页抓全文 |
| `max_fulltext_per_run` | `10` | 每次运行最多补几篇全文 |
| `list_retries` | `2` | 列表接口偶发风控时的重试次数 |
| `list_retry_delay_s` | `15` | 每次重试之间的等待秒数 |

### bilibili · `embed_player`

视频动态内嵌 B 站官方播放器，可以在阅读器里直接播：

```yaml
embed_player: html5player   # mobile | html5player | desktop | newplayer | false
```

| 值 | 播放页 | 视频源 | 实际结果 |
|----|--------|--------|----------|
| **`html5player`** | `blackboard/html5player.html` | MSE | **当前采用**。不带 `playsinline`，加载快、手机上能出来，双击可进 iOS 原生全屏且不黑屏 |
| `mobile` | `blackboard/html5mobileplayer.html` | 渐进 mp4 | 能播；但写死 `playsinline`，iOS 拿不到原生全屏（全屏会黑屏） |
| `desktop` | `player.bilibili.com/player.html` | MSE | 官方外链播放器，实测手机上出不来 |
| `newplayer` | `blackboard/player.html` | MSE | 卡在「播放器初始化」，出不来 |
| `false` | — | — | 不嵌播放器，靠正文里的视频标题链接 |

`desktop` 严格按[官方外链播放器文档](https://player.bilibili.com/player.html)的参数来
（只用文档列出的 `bvid` / `p` / `autoplay` / `danmaku` / `poster`）；`html5player` 没有官方文档，
用的是 `bvid` / `page` / `as_wide` / `high_quality` / `danmaku`。

> **选型依据（实测，不是在猜）**：
>
> - `desktop` 和 `html5player` **都走 MSE**（`<video>` 的 `src` 是 `blob:`），也**都不带 `playsinline`**，
>   理论上都能拿到 iOS 原生全屏。但两者是**不同的播放器实现**，实机上只有 `html5player` 出得来
>   （`desktop` 停在 `readyState: 0`，页面上一个字都没有）。
> - `mobile` 能播（喂渐进式 mp4），但它在 `mplayer.js` 里**无条件写死**了
>   `setAttribute("playsinline","")`，iOS 因此不暴露 `webkitEnterFullscreen`（原生全屏的唯一入口），
>   只能用 DOM 的 `requestFullscreen()` 自己做全屏 —— WKWebView 对这种 DOM 全屏支持很差，
>   退出时留下黑色图层，就是之前那个黑屏。
> - 自己渲染 `<video>` 绕过 iframe 行不通：直链（`upos-*.bilivideo.com/...mp4`）对任何 Referer
>   都返回 403（要播放器的 cookie 上下文），且 `deadline` 只有 2 小时。
>
> 切换只需改 `embed_player` 一行，改完立即生效。

转发动态里带的视频也会嵌（递归处理）。实测 34 条里 32 条带上播放器。

> ⚠️ **很多阅读器默认会把 `<iframe>` 过滤掉**（FreshRSS 的 HTML 白名单里就没有 iframe）。
> 过滤掉也不影响其它内容——视频标题链接和封面图都还在。
> 如果你的阅读器不显示播放器，就把它当成一个「点击跳转」的入口。

#### 已知限制：iOS 上全屏会黑屏

移动版播放器**能正常播放**，但在 iOS 的 WebView（NetNewsWire / FreshRSS App）里点全屏会出问题：
显示「正在全屏」，退出后整个文章变黑。原因查清楚了，**不在我们这边**：

```
video 元素属性（B 站移动版播放器）:
  playsinline / webkit-playsinline = ""   ← 声明内联播放，iOS 因此不启用原生全屏
  hasControls = false                     ← 控件是播放器自己画的
  webkitEnterFullscreen = undefined       ← ★ iOS 原生全屏入口根本不存在
  requestFullscreen = function            ← 改用标准 Fullscreen API
  data-fullscreen-container = "true"
```

B 站移动版播放器自己实现全屏：它在 `<video>` 上打了 `playsinline`，
于是 iOS 不暴露 `webkitEnterFullscreen`（原生全屏的唯一入口），
改成用 DOM 的 `requestFullscreen()` 在容器上做全屏。
WKWebView 对这种 DOM 全屏支持很差，退出时留下黑色图层。

这段逻辑在跨域的 `www.bilibili.com` iframe 里，**外面改不了**。
我们能做的只有把 iframe 的权限策略声明正确（已加
`allow="autoplay; fullscreen; encrypted-media; picture-in-picture"`），
但改变不了播放器自己的实现。

实际用法上绕开就行：**在阅读器里不要点全屏，看内联播放；要全屏就点视频标题链接跳去 B 站**，
那边是原生播放器，全屏正常。

确实受不了就把 `embed_player` 设成 `false`，只留标题链接 + 封面图。

### bilibili · 全文（`fulltext`）

**动态接口给的正文是截断的**：opus 内容（图文动态、文章）超过约 300 字，接口只回
开头一段，末尾补一个 `...`。全文在 opus 页（`https://www.bilibili.com/opus/<动态号>`）
里，而且那个页面是**服务端渲染**的 —— 实测直接 `fetch` 就能拿到 54 KB HTML，
`.opus-module-content` 里就是完整正文（那篇 2774 字）。所以不用像知乎那样开页面等
渲染，一个 `fetch + DOMParser` 就够，跨域也没问题（在 `t.bilibili.com` 上实测读得到）。

```
接口给的（288 字）: 考虑未来可能无法使用SpaceX载人龙飞船，NASA将采取若干关键措施…现役飞船。...
页面里的（2774 字）: 考虑未来可能无法使用SpaceX载人龙飞船，…原作者: Eric Berger
```

- 只补**真的被截断**的段落（以 `...` 结尾），一次运行最多补 `max_fulltext_per_run`
  （默认 10）篇；补过的打在 `extra.fulltext` 上，不会重抓。
- **只换那一段**：标题链接、图片、转发包装都原样留着。图片继续用接口给的那几张 ——
  opus 页的 HTML 里反而不带图（实测那篇 2774 字的文章，正文里一张 `<img>` 都没有，
  图片是客户端再插进来的），所以别拿页面正文去覆盖图片。
- 取全文的地址：段落**前面最近**的那个 opus 链接（转发里嵌的是**原动态**的）；
  没有链接而这条自己就是一篇 opus（图文 / 文章）时，用 `opus/<动态号>` ——
  实测 opus 号就是动态号。
- 摘要里的 `[图片]` 是占位（那个位置在正文里是图），页面正文里没这个标记，所以比对
  前先去掉它：拿摘要开头 80 字去页面正文里对，对不上就原样保留、**不标记**，
  下一轮再试 —— 万一是这里的选择器/比对规则写错了，改好之后存量条目自己就补上了。
- 视频动态（`DYNAMIC_TYPE_AV` / 合集更新）不参与：它的正文是视频简介，接口给的
  就是全文，末尾那个 `...` 常常是作者自己写的。

> ⚠️ 已经成型的**图片合集**补不了：合集是从成员动态拼出来的一条，里面那些截断正文
> 在合成那一刻就定死了。想要全文就删掉 state 里 `extra.kind == "image_digest"` 的
> 条目，让下一轮重新合成（成员条目自己已经补好的话，重新合成的合集里就是全文）。
>
> 另外和知乎一样，**阅读器里已经收到的条目不会跟着更新**（guid 没变），见
> 「阅读器那一侧的缓存」。

实测：一次 run 补完 10 篇，整个 run 2 秒（知乎那边一篇要开页面等 3~6 秒）。

### zhihu · `fulltext`

把动态的**预览摘要替换成全文**。抓取方式照搬 `kvxjr369f/zhihu_fulltext.py`：
导航到内容页 → 等正文渲染完 → 从 DOM 取正文。

几个关键设计：

- **每条只抓一次**。全文和 `extra.fulltext` 标记一起存进 `state/*.json`，
  之后不再重抓。所以正常情况下每次运行新增几条就只抓几篇（几秒钟）。
- **首次启用会分批补齐**。历史条目按 `max_fulltext_per_run` 每轮补一批，
  不会让某一次运行卡几分钟。
- 想法优先走 `api/v4/pins/<id>`（页面会随机重定向到别的想法），失败再退回页面抓取。
  回答和文章只能走页面——文章的 `api/v4/articles/<id>` 直接返回
  `403 请求参数异常`，接口拿不到。
- 重试：文章 4 次、回答/想法 2 次，间隔 10~12 秒。
- **「待补 N 篇」= 所有还没有 `extra.fulltext` 的条目**，一条都不少算。
  补不了的类型不会被悄悄跳过 —— 抓失败或没有对应的页面选择器都会打印出来，
  否则提示每轮都停在同一个数上，看着像卡死了。

支持的类型（`FULLTEXT_SELECTORS`）：

| 动态类型 | 正文取哪儿 |
|----------|------------|
| `answer` | 回答页 `.AnswerCard .RichText` |
| `article` | 文章页 `.Post-RichText` |
| `pin` | 优先 `api/v4/pins/<id>`，失败退回想法页 |
| `question` | 问题页的问题描述（`.QuestionRichText`） |

`question` 对应「添加了问题」这类动态：正文就是问题的**描述（补充说明）**。
它是折叠的（只给半截 + 「显示全部」按钮），`_extract_js` 会点开再取；
**问题可以没有描述**，那种情况页面上没这块内容，抓完按「页面没有正文」记一笔
（同样打 `fulltext` 标记），不会一直挂在待补里反复重抓。

这类动态的链接在接口里是 `https://api.zhihu.com/questions/<id>`（点开是一坨 JSON），
`_web_link()` 会换成 `https://www.zhihu.com/question/<id>`。条目 id 仍然用原始链接算
（见 `_item_id`），所以换链接不会让老条目变成新条目。

#### 正文不能取「第一帧」

长回答是**分阶段**渲染的：页面先给一小段，随后整篇才替换进来。原来的实现是
导航后固定等 3 秒再取 `.RichText` 的 `innerHTML`，实测会稳定地抓到那个半截版本，
而且因为**每条只抓一次**，半截版会被 `content_locked` 永久锁进 state：

```
酱紫君 《红警 2》的残酷电脑为什么在打完几波以后不继续发展了？
  抓到：2080 字符 HTML，正文 482 字（停在「…不得不变卖建筑。」）
  页面：4111 字符 HTML，正文 1573 字
```

现在改成在页面里轮询（`CONTENT_*` 几个常量）：长度连续 1.5 秒不变才认为渲染完，
至少等 3 秒、最多 20 秒，中途取到更长的就留着；顺手点掉折叠正文上的「阅读全文」
（只在按钮文字是 `阅读全文`/`展开` 时点，已展开时不会误触）。
取到的最长结果会被保留，所以万一展开反而让 DOM 变短，也不会把正文弄丢。

> ⚠️ **已经抓错的存量条目不会自己好**：它们带着 `extra.fulltext` / `content_locked`，
> 不会再被抓一次。要重抓，把这两个键从 `state/<id>.json` 里删掉即可
> （`dedup_key` 留着），下次运行会按 `max_fulltext_per_run` 分批补齐。
> 批量重置某一个源：
>
> ```bash
> python3 - <<'PY'
> from localrss.store import Store
> s = Store('state/zhihu-GalAster.json')
> items = s.load()
> for i in items:
>     if i.extra.get('fulltext'):
>         i.extra.pop('fulltext', None)
>         i.extra.pop('content_locked', None)
> s.save(items)
> PY
> ```
>
> 这条命令只**删标记**、不删条目（`save()` 不做裁切），重置后每条都要重新付一次抓取代价 ——
> 一个源 40 条、`max_fulltext_per_run: 10`，大概要四轮运行才补完。

配套修了个 state 的 bug：`Store.merge` 原本会用本次重新渲染的裸摘要覆盖老条目，
**把已经抓好的全文冲掉**，导致每一轮都在重抓同一批。
现在靠 `extra.content_locked` 标记保护——带这个标记的条目，其 `content` 不会被合并覆盖。
（好处是 bilibili 那边没这个标记，改了渲染逻辑后重新抓到的条目仍会正常刷新。）

正文长度实测：最长 36989 字符，中位 1515。state 约 200 KB，RSS 约 180 KB。

#### 图片别渲染两次

知乎正文的每个 `<figure>` 里其实有**两张一样的图**：一张在 `<noscript>` 兜底块里
（无 JS 时的降级图），另一张在 `RichText-ConditionalImagePortal` 里。阅读器一般会忽略
`<noscript>` 标签本身、却照常渲染里面的 `<img>`，于是同一张图渲染两次。

`_strip_noscript()` 把兜底块整块丢掉 —— 实测 248 张兜底图的地址在块外都能找到同一份，
不会丢图；块里含图以外内容的（比如 `<video>`），只拆掉标签、内容留着。

> 存量条目不用手动重置：`postprocess` 每轮都会对合并后的全量条目过一遍
> `_strip_noscript()`（幂等，重复跑无副作用），所以带重复图的老条目跑一轮就自己好了。
> 这点和全文不同 —— 全文抓错了必须删标记重抓（见上），这个只改渲染结果，不依赖重新抓取。

## 过滤与去重规则

过滤规则都在 `postprocess` 里对**最终列表**执行，
也就是作用在「历史状态 + 本次新增」合并之后 —— 所以改规则不用清状态文件，存量条目会立刻被重新筛一遍。
过滤只影响 RSS 输出，`state/<id>.json` 里始终保留全量，把开关关掉就全部回来。

### 过滤清单（`dropped_log`）：丢了哪些、为什么丢

日志里只会说「过滤掉 N 条」，看不出丢的是哪几条。想看明细就把 `output.dropped_log` 打开：

```yaml
output:
  log_dir: logs        # 清单写这里，可省略（默认 logs）
  dropped_log: true    # 默认 false
```

每个源写一份 `logs/<feed-id>.dropped.log`，**每次运行直接覆盖**（不是追加），
所以文件很小，等于「最近一轮过滤了什么」的快照：

```
# 过滤清单：每次运行覆盖写（生成于 2026-09-16 08:32:10）
# feed = zhihu-kvxjr369f (zhihu)  本轮 60 条 → 可见 39 条，过滤掉 21 条
# 原因 | 时间 | 标题 | 链接
关键词「关于每天的早安行动」 | 2026-09-15 07:02 | 发布了想法 | https://www.zhihu.com/pin/...
正文与另一条重复（dedupe_by_content） | 2026-09-14 22:41 | 收藏了回答 | https://www.zhihu.com/answer/...
正文早先已发布过（首次 2026-09-13T16:00，跨窗口去重） | 2026-09-16 11:33 | 收藏了回答 | https://www.zhihu.com/answer/...
```

- 覆盖写是有意的：始终只有一份、永远对应当前这轮。一条没丢时也会写一个空清单，
  免得上一轮的内容留在文件里冒充本轮结果（想比历史就把文件 `cp` 走）。
- 每条都带原因（命中的那个关键词 / 哪条规则丢的），用来核对关键词有没有误伤。
- 纯为人看的，跟 RSS 和 `state/` 无关；写失败只打一条 stderr，不影响本轮抓取。
- 明细里的条数应当等于终端里那个「过滤掉 N 条」——对不上就说明有条丢弃路径没记原因，可以提出来。

### 关键词过滤（全局 / 单个源）

在 `config.yaml` 顶层配一个列表，**对所有 feeds 生效**：

```yaml
exclude_keywords:
  - 妙界
  - 互动抽奖
  - 捞一下
```

只想拦某一个源（比如某个号的自动早安播报），就写在该 feed 自己的 `exclude_keywords` 里，
它和全局列表是**叠加**关系，只对这个源生效，别的源不受影响：

```yaml
feeds:
  - id: zhihu-kvxjr369f
    type: zhihu
    # ...
    exclude_keywords:
      - 关于每天的早安行动
      - 关于素问每日晚间抢答对战
```

匹配规则（全局和 feed 级完全一致）：

- 匹配范围是**标题 + 正文**（正文会先剥掉 HTML 标签）。
- **大小写不敏感**的子串匹配。`Rokid` 能命中 `rokid`，`APPLE LOG课程` 能命中 `Apple Log课程`。
- 图片 URL、`src` / `href` 这些 HTML 属性不参与匹配（整段标签被剥掉了），
  避免图片地址里恰好含关键词时误杀。
- 清空列表 = 关掉这个功能。

实现在 `Provider.postprocess()` 基类里，所以**任何站点都自动有**，
不需要每个 provider 各写一遍。两个列表在 `cli.py` 拼好之后交给 provider，
provider 拿到的始终是一个合并后的列表。

### bilibili · `filter_self_repost`

转发自己的动态（`作者 == 转发的原作者`，按 mid 判断）没有任何信息量，直接丢掉。
实测 57 条里筛掉 4 条；转发**别人**的照常保留。

### bilibili · `filter_image_only` 与动态合集

**先说结论：B 站的图片动态其实都有正文，之前「图文动态只剩图片」不是过滤器干的，
是接口少传了一个参数。** 详见下面的「图文动态的正文哪去了」。

`filter_image_only` 丢的是**真正的**纯图动态：有图片，但标题/正文/视频/文章/转发
一个都没有。判定用 `major.opus.summary`（新格式）或 `desc.text`（老格式），
所以带正文的图文不会被误伤。**只在 `image_digest_size: 0` 时才用得上这一项** ——
开了合集之后纯图走合集，不再直接丢。

`image_digest_size` 是它的替代方案（也是 `config.yaml` 里的做法）：

```yaml
      image_digest_size: 10            # 攒够 10 条图片动态/转发合成一条「合集」
      image_digest_max_age_hours: 24   # 攒不满的兜底有效期，0 = 不兜底
```

- `image_digest_size: 0`（代码默认）= 关掉合集，回到「纯图直接丢」的老行为；
- `> 0` = 图片动态和转发一律进合集，`filter_image_only` 不再参与。

### bilibili · 动态合集（`image_digest_size`）

**发图 + 转发**一条一条刷屏很吵，但直接丢掉又可惜。合集的做法是**攒够 N 条再合成一条**
进 RSS。实测关注流两天 118 条里：`DYNAMIC_TYPE_DRAW` 41 条（≈19/天）、
`DYNAMIC_TYPE_FORWARD` 14 条 —— 合计 55 条（≈25/天），够开 5 个合集，
省掉 50 条单条条目。

标题统一是 `动态合集 <作者…>`（这一批发过言的作者全列出来，去重、按时间顺序，不带条数）：

```
动态合集 旋风凉水、特厨魏味-、游戏星GameStar、卧烟同人社、ASPT-航天科普小组
```

正文里每条一段：`头像（show_avatar 开着时）· 作者（链到原动态）· 时间`，
下面接这条动态的正文和图片 —— **内容一条不落**，转发里的视频也会照常嵌播放器。

几条规则：

- **进合集的是**：`DYNAMIC_TYPE_DRAW`（发图，带不带正文都算）+ `DYNAMIC_TYPE_FORWARD`
  （转发）。判定看接口给的动态类型，所以 state 里的老条目也认得出，
  不依赖 `image_only` 标记（那个只看「有没有正文」）；
- **不进合集的是**：原创视频（`DYNAMIC_TYPE_AV`）、合集更新（`DYNAMIC_TYPE_UGC_SEASON`）、
  文章（`DYNAMIC_TYPE_ARTICLE`）、纯文字动态 —— 它们本来就是一条一条的正常内容；
- **先出旧的**：攒够 N 条就从最旧的开始切一批，剩下的继续等下一批；
- **兜底**：剩下的不足 N 条、且最旧那条已经超过 `image_digest_max_age_hours` 小时，
  就按现有条数照样出一条 —— 免得冷清的时候几条内容永远不出现在 RSS 里。
  设成 `0` 就是「一直攒着，攒够才出」。

每次运行都会报一下队列，攒到哪儿了一眼能看见：

```
  [动态合集] 合成 10 条：动态合集 旋风凉水、特厨魏味-、游戏星GameStar
  [动态合集] 在攒 7/10 条（还差 3 条）；最旧一条 14.8h 前，满 24h 也会照样发
```

**「合集怎么不更新」看这一行就够了**：它没坏，只是还在攒 —— 队列没满 N 条，
最旧那条也还没到兜底时限，所以这一轮不发。队列变慢最常见的原因是关注流里图文/转发
本来就少；另外「转发自 @自己」会被 `filter_self_repost` 提前丢掉，不进队列
（它们照样留在 state 里，只是永远不参与合集）。想发得更勤就调
`image_digest_size`（改小）或 `image_digest_max_age_hours`（改小）—— 都是下次运行生效。

实现上不需要额外的队列文件：合集是个普通条目，存进 `state/*.json`，
并且把「合成过哪几条」记在自己的 `extra.digest_ids` 里。所以

- 每次运行都是幂等的：同一批不会重复合成（`--force` 重抓也一样）；
- 没被合成的条目只是不进 RSS，state 里照旧留着，改小 `image_digest_size` 就会看到它们；
- **合集条目的正文也是每次运行重渲染的**（和单条一样先摘后加），所以改
  `show_avatar` / `embed_player` 对合集同样立即生效 —— 只有**标题和成员**是合成时定死的，
  想重新分批要删掉 state 里那些 `extra.kind == "image_digest"` 的条目。

> 顺带修了个播放器的老 bug：`_PLAYER_RE` 原来只认 `player.bilibili.com` 和
> `html5mobileplayer`，而 `config.yaml` 用的 `html5player` 落在 `blackboard/` 下 ——
> 于是「先摘旧的、再加回去」对它是失效的，**每跑一次就多插一个 iframe**
> （state 里能看到 `iframe x 3` 的条目）。现在正则覆盖四种播放器风格，重复运行稳定在 1 个。

### bilibili · 图文动态的正文哪去了（`features=itemOpusStyle`）

这个坑值得单独记一笔：**图文动态（`DYNAMIC_TYPE_DRAW`）现在有两套返回格式。**

| 请求 | `major.type` | 正文 | 图片 |
|---|---|---|---|
| 不带 `features` | `MAJOR_TYPE_DRAW` | **没有**（`desc` 是 `null`） | `major.draw.items` |
| `features=itemOpusStyle` | `MAJOR_TYPE_OPUS` | `major.opus.summary.text` | `major.opus.pics` |

网页版（t.bilibili.com）用的是后者，我们之前用的是前者 —— 于是所有图文动态
在本地都成了「只有图片、没有正文」，标题变成 `发布了 N 张图片`，
然后被 `filter_image_only` 当纯图丢掉。**正文其实一直都在，只是没问接口要。**

修法是给两个接口 URL 都加 `features=itemOpusStyle`（见 `OPUS_FEATURES`），
同时把 `_is_image_only` / `_title` 的「有没有正文/有没有图」改成认两套字段
（`_dynamic_text()` / `_pic_urls()`），否则 opus 格式下正文在 `opus.summary` 里，
只认 `desc.text` 还是会把带正文的图文误判成纯图。

用 WebBridge 在真实页面里核对过（`api.bilibili.com/.../detail` + 页面 DOM 对照）：
`opus.summary.text` 和页面上显示的正文一字不差，`has_more: false` 说明没被截断。

> ⚠️ 正文是抓取时渲染进 `content` / `title` 的，**存在 state 里**。
> 所以这次改动之后要 `./main.sh force` 重抓一遍，存量条目才会跟着更新。
> 又因为 force 只翻 `max_pages` 页，比这个窗口更老的条目不会重渲 ——
> 想让它们也补上，临时把 `max_pages` 调大（比如 6）跑一次 force，再改回来即可。

### bilibili · `show_avatar`

在每条正文最前面插入发帖 UP 主的头像（接口的 `module_author.face`）：

```html
<p><img src="https://i0.hdslb.com/bfs/face/7633....jpg" width="32" height="32" alt="苹果箱AppleBoxFilm"></p>
```

> **为什么是塞进正文，而不是改列表里那个头像？**
>
> RSS 2.0 **没有「每条一个图标」的字段**。阅读器列表里每条前面那个小图是 **feed 级 favicon**，
> FreshRSS 是按 channel `<link>` 的域名（`https://t.bilibili.com/`）去抓的，所以永远是 B 站的小电视，
> 跟你要不要换无关。能落地的位置只有正文。
>
> 单作者的 feed（知乎、或 bilibili 的 `mode: space`）倒是可以给 channel 加 `<image>` 来指定图标；
> 但「关注动态」有几十个 UP 主，没有单一头像可放。
>
> FreshRSS 那边也可以手动覆盖：订阅源设置里能直接指定图标。

顺带修了个隐性问题：接口给的图片地址有 `http://` 的（`face`、视频封面都是），
阅读器页面多半是 https，浏览器会把 http 图片当**混合内容**拦掉。
现在所有图片 URL 统一升级成 https（实测 `i0/i1/i2.hdslb.com` 都支持 https，返回 200 image/jpeg）。

### 保留条数（`history`）

`output.history` 是**全局默认**，每个 feed 可以用自己的 `history` 覆盖。
两者都是「条数」上限，不按体积算 —— 而带全文的源每条体积大得多，所以单独调小：

```yaml
output:
  history: 150      # 默认
feeds:
  - id: bilibili-follow
    history: 150    # 纯文本 + 封面，每条约 1.6 KB，满 150 条约 240 KB
  - id: zhihu-kvxjr369f
    history: 60     # 带全文，每条约 6.6 KB，满 60 条约 400 KB（150 条要 1 MB）
```

超过上限时按时间从旧到新裁掉。调小之后下一次运行就会立刻生效（`merge` 时切片）。

### zhihu 的列表重试

平台偶发风控时会返回 `请求参数异常，请升级客户端后重试。`，
以前只能等下一个 30 分钟的周期，现在会就地重试：

```yaml
    options:
      list_retries: 2          # 额外重试次数（总共 3 次尝试）
      list_retry_delay_s: 15   # 每次之间等 15 秒
```

重试会打日志：

```
[重试] 知乎动态第 1 页 第 1/2 次失败：知乎接口返回异常（第 1 页）: 请求参数异常，请升级客户端后重试。，15s 后重来
```

实现在 `localrss/providers/base.py` 的 `retry_call()`，其他 provider 也可以直接用。

### zhihu · `dedupe_by_content`

同一条回答会以两条动态出现，正文一字不差，只有动作行不同：

```
回答了问题：宿舍作息不一致时，怎样沟通才能既解决问题又不伤和气？
收藏了回答：宿舍作息不一致时，怎样沟通才能既解决问题又不伤和气？
   ↑ 正文完全相同，末尾链接也是同一个
```

按正文去重后只留**较早**的那条（`回答了问题`）。选早不选晚是有意的：
如果留晚的那条，guid 会从「回答了问题」变成「收藏了回答」，
RSS 阅读器会把它当成一条新内容重复提醒。

从渲染好的 `content` 里取正文（首段是动作、以 `<a ` 开头的是链接段，剩下的就是正文），
所以老的 `state/*.json` 也能直接吃这套规则，不需要迁移。

#### 去重指纹要跨窗口记（`dedupe_memory_days`）

**只比「窗口内的条目」不够，会漏掉一类重复。** 一对双胞胎里较早的那条通常就是
已经发出去、阅读器里已经有了的那条；它先被 `history` 裁掉之后，后一条成了孤家寡人 ——
它的 guid 由「动作 + 标题 + 链接 + 自己的时间」算出来（见 `_item_id`），
和已发过的那条**不一样**，于是同一篇文章被当成新条目又发了一遍。

实测就是这种情况（`logs/pm2-out.log`）：

```
2026-09-13T16:00:16:  [全文] 1/1 answer 2082498339958961570 (1987 字符)   ← 先发的是「回答了问题」
2026-09-16T12:01:10:  [全文] 4/4 answer 2082498339958961570 (2557 字符)   ← 三天后「收藏了回答」又发了一遍
2026-09-13T17:30:17:  [全文] 2/2 answer 2082517234208060113 (878 字符)
2026-09-16T13:00:36:  [全文] 2/2 answer 2082517234208060113 (1162 字符)
```

两次的正文 id 一样、条目 id 不同 —— 后一条是「收藏了回答」，前面那条被 `history`
挤出去之后再没人跟它比。这也是「知乎老是刷出旧文章」的由来。

所以指纹（`md5(正文)`）**单独存一份**：`state/<id>.json` 的 `seen_content`
（`{指纹: "首次发布时间|发布它的条目 id"}`）。条目会被 `history` 裁掉，这份表不会 ——
`dedupe_memory_days`（默认 45 天）到期的指纹才会被清掉。

值里的**发布条目 id 不能省**。只按指纹拦的话，窗口里那些「早就发过、而且还在窗口里」
的条目，下一轮会被当成「别人发过的同一份正文」全部丢掉 —— 每轮只剩新冒出来的几条，
feed 越跑越空（`41 条 → 可见 1 条` 那种）。有了发布者就分得清两种情况：

| 表里记的发布者 | 说明 | 处理 |
|---|---|---|
| 就是这条自己 | 一直在窗口里，只是又走了一遍 | 留着 |
| 另一条条目 id | 孪生里较早的那条发过、已经滚出窗口 | 丢掉 |
| 空（老格式只有时间） | 加发布者之前存的数据，拿不准 | 放行，并就地补写成新格式 |

- 判据还是**正文**。已经发过的正文再来一个动作（收藏 / 赞同），不再发第二遍，
  过滤清单里的原因是 `正文早先已发布过（首次 2026-09-13T16:00，跨窗口去重）`。
  **编辑不在此列** —— 编辑换的是 guid（新版本），见「zhihu · 编辑」。
- 记的是「**发出去过**」而不是「见过」：条目被关键词过滤掉不算发过。
- 表只在 RSS 输出这条路上起作用，`state` 里的条目一条不动；把 `dedupe_memory_days`
  调小或设 0，下次运行就会把表裁掉/清空，退回老行为（旧文章会再被重发一次）。
- 表的大小：够活跃的源一天几十条，45 天几百个指纹，几十 KB —— 比同源的 `items` 小得多。

⚠️ 判定依据是**正文文本**，不是链接。如果同一个人先后发了两条文字一模一样的想法，
也会被合并成一条 —— 这正是「按 description 去重」的含义，但确实会合并掉两个不同的
pin ID。不想要这个行为就把 `dedupe_by_content` 设成 `false`；
只是不想记那么久，就调小 `dedupe_memory_days`（比如 `7`）。

### zhihu · 编辑（`updated`）

知乎的回答 / 文章 / 想法被作者编辑后**不会多出一条动态**：动态流里那条旧动态只是
`target.updated_time` 变成了编辑时间。这个字段原先没取，所以本地永远停在编辑前那一版。

现在：

- `updated` 取 `target.updated_time`（回答 / 文章）或 `target.updated`（想法，没有 `_time`
  后缀），存进 `extra.updated`；问题类动态的 target 不带这个字段，取到 0。
- **它要编进 guid**：`zhihu:<发布时间>_<编辑时间>_<哈希>`。内容被编辑后 guid 跟着变，
  阅读器才会把新版本当成新条目收下来 —— guid 不变的话它不会去刷新已经收过的条目
  （见「阅读器那一侧的缓存」那节）。拿不到 `updated` 的类型退回 `zhihu:<发布时间>_<哈希>`。
- 全文会自动重抓：新版本是个新条目，身上没有 `extra.fulltext` 标记。
- **`<pubDate>` 取原发布时间和编辑时间里较晚的那个**（`extra.updated`）：阅读器大多按
  pubDate 排序和提醒，还用原发布时间的话，编辑后的新版本会被埋在原来的位置里，等于
  白收一条。（「先编辑、后赞同」那种动态比编辑时间还晚，取较晚的才不会让 pubDate 往回跳。）
- feed 里条目的**顺序**、`history` 裁剪、去重「留早的那条」仍然按 `item.published`
  （原发布时间）走，这几处不能跟着换 —— 孪生（回答了问题 / 收藏了回答）带的是同一个
  `updated`，按它排序两条会打平，「留早的」就失效了：留下的那条随运行漂移、guid
  跟着换，阅读器里反而会重复提醒。
- 代价是阅读器里新旧两版都在 —— 旧的那条收下来之后就删不掉了，只能自己标记已读。

#### 旧版本要清掉（`prune_superseded`）

换了 guid 之后，state 里会同时留着旧版本（旧 id）和新版本。动态接口只返回**当前版本**，
那份旧版本再也不会被刷新，标题和链接还跟新版本一模一样 —— 留着只会白占 `history`
的位置、在 RSS 里显示成一条重复条目。所以每轮合并进 state 之后、过滤之前会清一次，
按「**同一条内容的身份** = 链接 + 动作」判断，同身份里 `updated` 最大的那份才留：

```
[编辑] 清掉 30 条被新版本取代的旧条目
```

- 同一篇回答的「回答了问题 / 收藏了回答」动作不同，算两个身份，不会互相清掉。
- 顺手把加 `updated` 之前存下的老条目也一起换了：同身份既然已经有带 `updated` 的
  当前版本，那条没有 `updated` 的就是同一份内容的历史遗留（id 形式换过一次）。
- 清掉的条目**不算「过滤掉」**、也不进过滤清单 —— 它们不是被规则筛掉的，
  而是站点上已经不存在了。
- 实现在 `zhihu.py` 的 `prune_superseded()`，由 cli 在 `postprocess` 之前调用；
  这个钩子的默认实现什么都不做（`base.py`），所以别的站点不受影响。

> 迁移：这次改动上线后第一次运行会把窗口里的条目全当成新条目（id 形式变了），
> 阅读器收到一轮「新」条目，之后增量抓取照常。旧版本连同它登记的指纹一起作废，
> 不用手动清 `state/`。

## 新增一个网站

1. 在 `localrss/providers/` 下新建 `example.py`，实现 `Provider` 子类并注册：

   ```python
   from ..bridge import Bridge
   from ..models import Item
   from . import register
   from .base import Provider

   @register
   class ExampleProvider(Provider):
       type = "example"
       description = "示例站点"

       def page_url(self) -> str:
           # 抓取时浏览器需要停留的页面：必须与目标接口同源，
           # 并且带上你的登录 cookie
           return "https://example.com/"

       def fetch(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
           # 用 bridge.evaluate(...) 在页面里跑 JS，返回 Item 列表。
           # known_ids 是本地已有 ID，用来做增量：整页都是已有时即可 break。
           ...
   ```

2. 在 `config.yaml` 里加一个 `type: example` 的源。

`providers/` 目录下的模块会被自动导入，不需要改注册表。

写 `fetch` 的两个要点：

- **先让浏览器停在正确的页面**。`evaluate` 作用于「当前标签页」，
  基类的 `setup()` 已经根据 `page_url()` 切好页；如果改用别的页面，记得同步改 `page_url()`。
  跨域请求会直接 `Failed to fetch`，所以页面和接口必须同源（或接口允许该来源跨域）。
- **只把需要的字段取回来**。在 JS 里就把响应压成扁平结构再返回，
  不要把原始 JSON 整个搬回来（B 站一页原始数据约 130 KB）。
- **过滤/去重写在 `postprocess()` 里**，别写在 `fetch()` 里。`fetch()` 只拿到本次新条目，
  而 `postprocess()` 拿到的是合并后的完整列表，改规则时存量数据也会被重新筛一遍。
  覆盖 `postprocess()` 时记得先 `super().postprocess(items)`，
  基类那次调用负责关键词过滤（全局 + 该源自己的 `exclude_keywords`）。
  参考 `bilibili.py` 的 `filter_self_repost` 和 `zhihu.py` 的 `dedupe_by_content`。
  丢条目时用 `self.drop_unless(items, keep, reason)`（或直接 `self.note_drop(item, reason)`），
  这样过滤清单日志里也能看到这条是被谁丢的（见「过滤清单」）。

## 文件结构

```
local_rss/
├── config.yaml              # 源配置（抓什么）+ base_url + hub_url
├── ecosystem.config.js      # pm2 配置（三个进程、cron_restart 间隔、端口）
├── rss.py                   # 抓取入口
├── rss-hub.py               # WebSub hub 入口（一般由 pm2 的 local-rss-hub 拉起）
├── run.sh                   # 定时抓取入口（补 PATH + 确保 WebBridge 在跑）
├── main.sh                  # 本地手动入口（force/clean/ping/status/log/restart）
├── serve.sh                 # 常驻订阅服务（静态提供 output/*.xml）
├── hub.sh                   # 常驻 WebSub hub（本地绑 127.0.0.1:8667）
├── requirements.txt
├── deploy/
│   └── com.localrss.refresh.plist   # launchd 定时任务模板（备选方案）
├── localrss/
│   ├── bridge.py            # WebBridge 客户端（标签页管理 / evaluate / fetch / 清理）
│   ├── config.py            # YAML 解析
│   ├── models.py            # 统一的 Item 结构
│   ├── store.py             # 状态与去重
│   ├── feed.py              # RSS 2.0 生成（含 rel="hub" 声明）
│   ├── websub.py            # WebSub 发布端：内容更新后 ping hub
│   ├── hub.py               # 自用的极简 WebSub hub（纯标准库）
│   ├── cli.py               # 命令行逻辑
│   └── providers/
│       ├── __init__.py      # 注册表，自动发现同目录模块
│       ├── base.py          # Provider 基类
│       ├── bilibili.py
│       └── zhihu.py
├── output/                  # 生成的 RSS
├── state/                   # 每个源一份 JSON（条目 + 跨窗口去重指纹）+ hub.json（WebSub 订阅关系）
└── logs/                    # pm2 / launchd / cron 的输出 + <feed-id>.dropped.log 过滤清单
```

## 常见问题

**提示 WebBridge 未就绪** — 启动 daemon 并确认扩展已连接：

```bash
~/.kimi-webbridge/bin/kimi-webbridge start
~/.kimi-webbridge/bin/kimi-webbridge status
```

**抓到的条数为 0，或接口返回异常** — 多半是浏览器里没登录（或被要求验证）。
打开页面确认一下，登录后重跑即可。

**知乎返回「请求参数异常，请升级客户端后重试」** — 这是知乎的风控/限频响应，不是代码问题。
短时间内在同一账号上反复抓取（比如连续手动重跑、或间隔设得太密）就会偶发。
等几分钟再跑即可，长期就把 `cron_restart` 调到 30 分钟以上。

**跑完 `pm2 list` 显示 `stopped`** — 正常。这是一次性脚本，跑完就该退出，到点会自己再跑。

**加了关键词但没看到条数变化** — 说明当前抓取窗口（`max_pages` 覆盖的范围）里还没有命中这些关键词的条目。
过滤是对已抓到的条目生效的，命中要等这些内容真的出现在动态流里。
想确认规则本身有没有写对，可以往 `state/<id>.json` 里搜一下关键词：
`grep 妙界 state/bilibili-follow.json`。

**想强制重新抓全量** — 删掉对应的状态文件，例如 `rm state/bilibili-follow.json`。

**B 站关注动态里混进直播推荐** — 这类没有正文的卡片会被自动跳过。
