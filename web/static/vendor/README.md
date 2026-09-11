# 前端第三方资源（本地化）

应用运行时不依赖任何 CDN：以下资源随仓库分发，离线可用。
（导出的报告 HTML 是发给别人看的，`report.html` 会在导出时把这些路径换回 CDN。）

| 文件 | 版本 | 许可证 | 来源 |
|---|---|---|---|
| bootstrap.min.css | Bootstrap 5.3.2 | MIT | https://github.com/twbs/bootstrap |
| bootstrap.bundle.min.js | Bootstrap 5.3.2 | MIT | 同上（内含 @popperjs/core，MIT） |
| jquery.min.js | jQuery 3.7.1 | MIT | https://github.com/jquery/jquery |
| echarts.min.js | ECharts 5.6.0 | Apache-2.0 | https://github.com/apache/echarts |
| echarts-wordcloud.min.js | echarts-wordcloud 2.1.0 | MIT | https://github.com/ecomfe/echarts-wordcloud |

各文件头部自带许可证注释；`echarts-wordcloud.min.js.LICENSE.txt` 是其打包的
wordcloud2.js 许可证附件（构建产物内声明，文件名必须保持原样）。

获取方式（升级时重新下载并核对版本）：

```bash
curl -o bootstrap.min.css           https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css
curl -o bootstrap.bundle.min.js     https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js
curl -o jquery.min.js               https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js
curl -o echarts.min.js              https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js
curl -o echarts-wordcloud.min.js    https://cdn.jsdelivr.net/npm/echarts-wordcloud@2.1.0/dist/echarts-wordcloud.min.js
```
