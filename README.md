# 杭州电子科技大学图书馆抢座脚本

## 脚本介绍

本脚本用于杭电图书馆自习室座位预约，目前支持自动登录、批量预约、定时预约等功能，有以下模块：

* 查看/添加/删除待选座位方案
* 批量修改方案中预约时间
* 定时抢座
* 图形化界面

**本脚本仅限用于个人图书馆预约座位，请勿恶意囤座位！**

**截至2026-08-16本脚本还可正常使用**

## 运行说明

0. 本脚本基于Python 3.14编写，请先安装Python 3.14。
1. 克隆本项目

``` shell
git clone https://github.com/stormmmg/HDU-Library-SeatHunter.git
cd HDU-Library-SeatHunter
```

2. 安装依赖项

```shell
pip install -r requirements.txt
```

3. 运行脚本

``` shell
python main.py
```

4. 构建 exe

```
python build.py
```

## GitHub Actions 自动预约

项目内置了 `.github/workflows/main.yml`。工作流每天北京时间 15:00
启动，先完成登录和 UID 验证，再在 GitHub Runner 内等待。19:48 验证会话，
`booking_open_time`（20:00:00）到达后才发出第一个预约请求，抢两天后的座位，
到 `booking_deadline`（20:15）收手退出。后续请求起点至少相隔 4.2 秒，响应耗时
计入该间隔；若收到限流响应，则约 1 秒后短探测，因为现有日志没有显示该响应
会追加惩罚时长。开放前不会发送预约请求，以免消耗开闸时的首个受理额度。
登录或 UID 验证遇到临时故障时最多尝试 3 次，
重试前约等待 30 秒、60 秒；明确的账号密码错误不会重试。

1. 先在本地运行 GUI，完成登录并添加真实的预约方案和调度。
2. 将本地 `config/config.yaml` 中的 `plans`、`schedules` 复制到
   `config/ci.yaml`，清空其中的 `login_name` 和 `password`。
3. 检查 `config/ci.yaml` 中不存在“替换为……”占位文本，然后把调度的
   `enabled` 改为 `true` 并推送到 GitHub。
4. 在 GitHub 仓库的 `Settings → Secrets and variables → Actions` 添加：
   - `SCHOOL_ID`：学号
   - `PASSWORD`：统一身份认证密码
5. 在仓库 `Actions` 页面启用工作流，并先用 `Run workflow` 手动验证一次。

GitHub 的 cron 只接受 UTC，`0 7 * * *` 即北京时间 15:00；工作流里的 `TZ`
只影响 Runner 内的 Python，不影响触发时刻。

之所以提前 5 小时启动，是因为 GitHub 的定时任务排队延迟很大——本仓库的历史
记录显示实际触发比标称时间晚 14 分钟到 4 小时 37 分，从未准时过。多出来的时
间由脚本在 Runner 内空等，`timeout-minutes: 350` 保证等待期间 job 不会被杀
掉。公开仓库的 Actions 分钟数免费，空等没有额外成本。

若延迟大到 20:15 之后才启动，脚本会直接退出而不发预约请求，Actions 上显示为
失败——这表示错过了窗口，而不是抢座失败。GitHub 单个 job 上限 6 小时，所以
提前量最多只能再加约 30 分钟。

> 公开仓库若连续 60 天没有任何活动，GitHub 会自动停用其定时任务，需要到
> Actions 页面手动重新启用。

`config/ci.yaml` 只保存座位和调度，不应提交账号、密码或 Cookie。

> GitHub 定时任务可能延迟。`--once` 模式如果在开放时间之后才启动，会立即
> 尝试预约；没有匹配两天后日期的已启用调度时会正常退出，不发送预约请求。

最后根据软件提示登录、查看使用说明。

本脚本基于https://github.com/LittleHeroZZZX/hdu-library-killer改进

最后请各位善用脚本，祝愿各位校友前途似锦，终成所愿。
