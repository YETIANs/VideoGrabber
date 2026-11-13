# 视频提取器 · VideoGrabber

一个完全跑在本地的视频/音频下载工具。双击启动一个小服务，浏览器里贴链接即可把视频存到本机。

> 只用来自行备份和学习，下载内容的版权归原平台与作者所有。

## 特性

- **纯本地运行**：服务只监听 `127.0.0.1`，解析、下载、合并全部在本机完成，不上传任何数据
- **双通道解析**：国内平台走解析站接口，海外平台走 yt-dlp（覆盖 1800+ 站点）
- **画质可选**：多档画质列表，自动挑 H.264 + AAC 保证播放器兼容
- **音视频合并**：内置 ffmpeg，分离的 DASH 流自动合成 mp4
- **MP3 导出**：单独提取音轨转码为 mp3
- **平台快捷入口**：点平台按钮直接跳转，复制链接后自动识别
- **登录状态同步**：配套浏览器扩展，一键把登录态同步给本地服务
- **零第三方 Web 依赖**：后端用 Python 标准库 `http.server`，无需 Flask/FastAPI

## 支持的平台

| 类别 | 平台 |
|---|---|
| 实测可用 | B站、YouTube、网易云音乐 |
| 国内 | 抖音、小红书、快手、微博、优酷、爱奇艺、腾讯视频、知乎 |
| 海外 | TikTok、Instagram、Facebook、Reddit、Twitch、Vimeo、SoundCloud、Bandcamp 等 |
| 需登录 | X、QQ音乐、Vimeo、B站高清 |

## 快速开始

```bash
# 依赖：Python 3.9+、ffmpeg、yt-dlp
pip install -U yt-dlp

python server.py
# 浏览器打开 http://127.0.0.1:8765
```

Windows 上直接双击 `启动.bat`。

## 目录结构

```
server.py          后端：HTTP 路由、双通道解析、下载与合并
index.html         前端：单页界面
extension/         浏览器扩展（同步登录状态）
installer.iss      Inno Setup 打包脚本
启动.bat           Windows 启动脚本
```

运行时会在程序目录（或 `%LOCALAPPDATA%\VideoGrabber`）生成：

```
config.json    保存路径配置
cookies.txt    登录凭证（敏感，已在 .gitignore 中排除）
downloads/     下载的视频
```

## 安全设计

- 只绑 `127.0.0.1`，局域网访问不到
- 所有写操作校验 `Origin`，挡跨站请求（CSRF）
- 只接受公开的 `http/https` 链接，内网地址一律拒绝（防 SSRF）
- 文件读取用 `basename` 白名单，防路径穿越
- cookie 字段过滤 `\t \r \n`，防格式注入
- 临时文件放 `.parts/` 子目录，完成后才移到正式位置

## 打包

```bash
# 单文件 exe（内含 Python、yt-dlp、ffmpeg）
pyinstaller --onefile --name VideoGrabber ^
  --add-data "index.html;." --add-data "extension;extension" ^
  --add-binary "ffmpeg.exe;." --collect-all yt_dlp server.py

# 安装程序
ISCC installer.iss
```

## 说明

- 解析第三方站点靠的是它们的公开接口，站点改版可能失效
- Chrome 137 起禁止程序自动安装扩展，扩展需要手动加载一次
- 请控制请求频率，不要给别人的服务器造成压力
