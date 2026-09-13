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
./main.sh status           # pm2 进程 / 订阅地址 / 数据概览
./main.sh log 50           # 最近 50 行抓取日志
./main.sh restart          # 重启 pm2 抓取任务（等于立刻跑一次）
```

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
`max_fulltext_per_run` 分批重新补齐。

## 改了配置多久生效

| 改的东西 | 生效方式 |
|----------|----------|
| `exclude_keywords` | **立即** |
| `filter_self_repost` / `filter_image_only` | **立即** |
| `dedupe_by_content` | **立即** |
| `show_avatar` / `embed_player` | **立即** |
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
我们的 guid 是稳定的（`bilibili:<动态id>` / `zhihu:<时间戳>_<哈希>`），
所以**改了渲染结果后，阅读器里已经存在的条目还是显示旧内容** —— 这跟本地 XML 无关。

想在看过的阅读器里看到新内容，得在阅读器里**删掉这个订阅源再重新添加**。
（FreshRSS：订阅管理 → 删除 → 重新订阅同一个地址。）

这次的播放器就是典型：本地 XML 已经是移动版播放器了，但手机上如果还是旧内容，
那多半是阅读器缓存，不是播放器本身的问题。

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

`config.yaml` 里的 `output.base_url` 要和这个端口保持一致，
它决定 RSS 内部的 `<atom:link rel="self">`：

```yaml
output:
  base_url: http://127.0.0.1:8666
```

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

`config.yaml` 里的 `output.base_url` 只是 RSS 内部的 `<atom:link rel="self">`，
不影响抓取；如果阅读器不介意，留 `127.0.0.1` 也没问题。

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

pm2 管两个进程：

| 进程 | 作用 | 特性 |
|------|------|------|
| `local-rss` | 定时抓取 | 跑完就退出，`cron_restart` 到点重跑，`pm2 list` 里显示 `stopped` 是正常的 |
| `local-rss-http` | 常驻订阅服务 | `autorestart: true`，挂了自动拉起 |

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
  history: 150       # 每个源最多保留多少条（可被各 feed 的 history 覆盖）
  base_url: ""       # 可选，服务地址前缀

# 全局关键词过滤：标题或正文命中任一关键词的条目会被丢掉。
# 对所有 feeds 生效，不想要就清空这个列表。
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
| `filter_image_only` | `true` | 过滤掉纯图动态（只有图片、没有正文的） |
| `show_avatar` | `true` | 在每条正文开头放 UP 主头像（改开关立即生效） |
| `embed_player` | `html5player` | 视频内嵌播放器：`mobile` / `html5player` / `desktop` / `newplayer` / `false`（改开关立即生效） |

### bilibili 的标题格式

投稿视频的标题是 **`作者 + 空格 + 视频标题`**，不带动作前缀：

```
苹果箱AppleBoxFilm 你不是混的很好吗？钱呢！
鞑厨高寒 土豆的国外花样
```

这样光看标题就知道是谁发的（很多阅读器不会把 `dc:creator` 显示在列表里）。
其余类型保持原样：`发布了文章：…`、转发取转发的正文首行、纯文字动态取正文首行。

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
| `dedupe_by_content` | `true` | 正文相同的只保留一条 |
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

### zhihu · `fulltext`

把动态的**预览摘要替换成全文**。抓取方式照搬 `kvxjr369f/zhihu_fulltext.py`：
导航到内容页 → 等 3 秒渲染 → 从 DOM 取正文。

几个关键设计：

- **每条只抓一次**。全文和 `extra.fulltext` 标记一起存进 `state/*.json`，
  之后不再重抓。所以正常情况下每次运行新增几条就只抓几篇（几秒钟）。
- **首次启用会分批补齐**。历史条目按 `max_fulltext_per_run` 每轮补一批，
  不会让某一次运行卡几分钟。
- 想法优先走 `api/v4/pins/<id>`（页面会随机重定向到别的想法），失败再退回页面抓取。
  回答和文章只能走页面——文章的 `api/v4/articles/<id>` 直接返回
  `403 请求参数异常`，接口拿不到。
- 重试：文章 4 次、回答/想法 2 次，间隔 10~12 秒。

配套修了个 state 的 bug：`Store.merge` 原本会用本次重新渲染的裸摘要覆盖老条目，
**把已经抓好的全文冲掉**，导致每一轮都在重抓同一批。
现在靠 `extra.content_locked` 标记保护——带这个标记的条目，其 `content` 不会被合并覆盖。
（好处是 bilibili 那边没这个标记，改了渲染逻辑后重新抓到的条目仍会正常刷新。）

正文长度实测：最长 36989 字符，中位 1515。state 约 200 KB，RSS 约 180 KB。

## 过滤与去重规则

过滤规则都在 `postprocess` 里对**最终列表**执行，
也就是作用在「历史状态 + 本次新增」合并之后 —— 所以改规则不用清状态文件，存量条目会立刻被重新筛一遍。
过滤只影响 RSS 输出，`state/<id>.json` 里始终保留全量，把开关关掉就全部回来。

### 关键词过滤（全局）

在 `config.yaml` 顶层配一个列表，**对所有 feeds 生效**：

```yaml
exclude_keywords:
  - 妙界
  - 互动抽奖
  - 捞一下
```

匹配规则：

- 匹配范围是**标题 + 正文**（正文会先剥掉 HTML 标签）。
- **大小写不敏感**的子串匹配。`Rokid` 能命中 `rokid`，`APPLE LOG课程` 能命中 `Apple Log课程`。
- 图片 URL、`src` / `href` 这些 HTML 属性不参与匹配（整段标签被剥掉了），
  避免图片地址里恰好含关键词时误杀。
- 清空列表 = 关掉这个功能。

实现在 `Provider.postprocess()` 基类里，所以**任何站点都自动有**，
不需要每个 provider 各写一遍。

### bilibili · `filter_self_repost`

转发自己的动态（`作者 == 转发的原作者`，按 mid 判断）没有任何信息量，直接丢掉。
实测 57 条里筛掉 4 条；转发**别人**的照常保留。

### bilibili · `filter_image_only`

纯图动态：只有图片、没有正文，也没有视频/文章/转发等其他内容。
实测 57 条里有 **20 条**属于这类（占了三分之一），标题会变成「发布了 N 张图片」那种。

判定依据是「有图片 **且** 除图片段外没有任何其他内容」，所以**带文字的图照样保留**，
只是碰巧有封面图的视频也不会被误伤。开关关掉就是 57 条里的 53 条（只掉自转发）。

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

⚠️ 判定依据是**正文文本**，不是链接。如果同一个人先后发了两条文字一模一样的想法，
也会被合并成一条 —— 这正是「按 description 去重」的含义，但确实会合并掉两个不同的
pin ID。不想要这个行为就把 `dedupe_by_content` 设成 `false`。

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
  基类那次调用负责全局关键词过滤。
  参考 `bilibili.py` 的 `filter_self_repost` 和 `zhihu.py` 的 `dedupe_by_content`。

## 文件结构

```
local_rss/
├── config.yaml              # 源配置（抓什么）+ base_url
├── ecosystem.config.js      # pm2 配置（cron_restart 间隔、订阅服务端口）
├── rss.py                   # 抓取入口
├── run.sh                   # 定时抓取入口（补 PATH + 确保 WebBridge 在跑）
├── main.sh                  # 本地手动入口（force/clean/status/log/restart）
├── serve.sh                 # 常驻订阅服务（静态提供 output/*.xml）
├── requirements.txt
├── deploy/
│   └── com.localrss.refresh.plist   # launchd 定时任务模板（备选方案）
├── localrss/
│   ├── bridge.py            # WebBridge 客户端（标签页管理 / evaluate / fetch / 清理）
│   ├── config.py            # YAML 解析
│   ├── models.py            # 统一的 Item 结构
│   ├── store.py             # 状态与去重
│   ├── feed.py              # RSS 2.0 生成
│   ├── cli.py               # 命令行逻辑
│   └── providers/
│       ├── __init__.py      # 注册表，自动发现同目录模块
│       ├── base.py          # Provider 基类
│       ├── bilibili.py
│       └── zhihu.py
├── output/                  # 生成的 RSS
├── state/                   # 每个源一份 JSON，用于去重
└── logs/                    # pm2 / launchd / cron 的输出
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
