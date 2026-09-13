// pm2 配置 —— local_rss
//
// 三个进程：
//   local-rss       定时抓取（跑完就退出，按 cron_restart 重跑）
//   local-rss-http  常驻静态服务，把 output/*.xml 暴露成订阅地址
//   local-rss-hub   常驻 WebSub hub，让 FreshRSS 能立刻收到更新
//
//   pm2 start ecosystem.config.js     # 启动（抓取任务会立刻跑一次）
//   pm2 save                          # 存下进程列表
//   pm2 logs local-rss                # 看抓取日志
//   pm2 logs local-rss-http           # 看 HTTP 服务日志
//   pm2 logs local-rss-hub            # 看 hub 日志（订阅/推送都打这里）
//   pm2 restart local-rss             # 手动立刻抓一次
//   pm2 restart local-rss-http        # 改了端口后重启服务
//   pm2 delete local-rss local-rss-http local-rss-hub
//
// ⚠️ local-rss 是「跑完就退出」的一次性脚本，autorestart 必须为 false。
//    否则 pm2 会把正常退出当成崩溃，无脑重启刷屏。
//    跑完在 `pm2 list` 里显示 stopped 是正常状态，等 cron_restart 到点再跑。

module.exports = {
  apps: [
    {
      name: 'local-rss',
      script: './run.sh',
      interpreter: 'bash',
      cwd: __dirname,

      // 一次性任务：退出后不要自动重启
      autorestart: false,

      // 刷新间隔 —— 改这里。标准 5 段 cron（分 时 日 月 周），本地时区。
      //   每 30 分钟 : '*/30 * * * *'      （推荐）
      //   每 15 分钟 : '*/15 * * * *'
      //   每小时整点 : '7 * * * *'          （避开 :00 整点）
      //   每天 8:05  : '5 8 * * *'
      cron_restart: '*/30 * * * *',

      // 日志
      time: true,
      merge_logs: true,
      out_file: './logs/pm2-out.log',
      error_file: './logs/pm2-err.log',
    },

    {
      name: 'local-rss-http',
      script: './serve.sh',
      interpreter: 'bash',
      cwd: __dirname,

      // 常驻服务，挂了要拉起来
      autorestart: true,

      env: {
        // 订阅地址的端口 —— 改这里后 `pm2 restart local-rss-http`
        PORT: '8666',
        // 只本机可访问；想让手机/平板订阅改成 '0.0.0.0'
        // （注意：那会把 RSS 暴露给同局域网的所有设备）
        HOST: '127.0.0.1',
      },

      time: true,
      merge_logs: true,
      out_file: './logs/pm2-http-out.log',
      error_file: './logs/pm2-http-err.log',
    },

    {
      name: 'local-rss-hub',
      script: './hub.sh',
      interpreter: 'bash',
      cwd: __dirname,

      // 常驻服务，挂了要拉起来
      autorestart: true,

      env: {
        // WebSub hub 的端口 —— 改这里后 `pm2 restart local-rss-hub`
        HUB_PORT: '8667',
        // 只本机；对 tailnet 暴露靠 `tailscale serve --bg --tcp=8667 8667`
        HUB_HOST: '127.0.0.1',
      },

      time: true,
      merge_logs: true,
      out_file: './logs/pm2-hub-out.log',
      error_file: './logs/pm2-hub-err.log',
    },
  ],
};
